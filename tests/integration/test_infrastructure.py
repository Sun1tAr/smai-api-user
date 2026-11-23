import json
import os
import subprocess

import httpx
import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from sqlalchemy import text

from user.config import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]


async def test_startup_health_metrics_and_async_session(settings: Settings) -> None:
    from user.main import create_app

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            health = await client.get("/health")
            metrics = await client.get("/metrics")
            schema = await client.get("/openapi.json")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        assert metrics.status_code == 200
        assert "http_requests_total" in metrics.text
        assert "/metrics" not in schema.json()["paths"]
        async with app.state.session_factory() as session:
            assert await session.scalar(text("SELECT 1")) == 1


async def test_metrics_use_the_registry_stored_in_app_state(settings: Settings) -> None:
    from prometheus_client import Counter

    from user.main import create_app

    app = create_app(settings)
    Counter("domain_probe", "Probe", registry=app.state.metrics_registry).inc()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            metrics = await client.get("/metrics")
    assert "domain_probe_total 1.0" in metrics.text


def test_alembic_upgrade_and_downgrade_use_configured_database(
    alembic_env: dict[str, str],
) -> None:
    for arguments in (
        ["upgrade", "head"],
        ["downgrade", "base"],
        ["upgrade", "head"],
        ["check"],
        # БД возвращается в исходное состояние для остальных интеграционных тестов.
        ["downgrade", "base"],
    ):
        result = subprocess.run(
            ["alembic", *arguments],
            env=alembic_env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


async def test_http_and_database_traces_correlate_with_json_logs(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from user.main import create_app

    app = create_app(settings)

    # /health намеренно не трассируется, поэтому проверяем обычным маршрутом.
    @app.get("/trace-probe")
    async def trace_probe() -> dict:
        async with app.state.session_factory() as session:
            await session.execute(text("SELECT 1"))
        return {}

    exporter = InMemorySpanExporter()
    app.state.tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/trace-probe")
        spans = exporter.get_finished_spans()
        server = next(span for span in spans if span.kind == SpanKind.SERVER)
        assert any(
            span.attributes.get("db.statement") == "SELECT 1"
            and span.context.trace_id == server.context.trace_id
            for span in spans
        )
        output = capsys.readouterr().out
        entries = [json.loads(line) for line in output.splitlines()]
        request_log = next(
            item for item in entries if item["message"] == "http_request"
        )
        assert request_log["path"] == "/trace-probe"
        assert request_log["trace_id"] == f"{server.context.trace_id:032x}"
        assert request_log["request_id"] == response.headers["X-Request-ID"]
        assert request_log["service"] == "smai-api-user"
        assert request_log["level"] == "info"
        assert request_log["timestamp"]
        assert settings.db_password.get_secret_value() not in output
