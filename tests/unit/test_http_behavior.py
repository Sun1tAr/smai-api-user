import json
import sys

import httpx
import pytest
from fakes import unavailable_db_settings
from fastapi import Query
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel


class Payload(BaseModel):
    count: int


class Inner(BaseModel):
    values: list[int]


class Outer(BaseModel):
    inner: Inner


@pytest.fixture
async def app(capsys):
    from user.main import create_app

    app = create_app(unavailable_db_settings())

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("SELECT secret-text FROM /var/lib/db")

    @app.get("/items/{item_id}")
    async def item(item_id: int, limit: int = Query(default=1)) -> dict:
        return {"id": item_id}

    @app.post("/nested")
    async def nested(body: Outer) -> dict:
        return {}

    @app.post("/payload")
    async def payload(body: Payload) -> dict:
        return {"count": body.count}

    yield app
    await app.state.engine.dispose()
    app.state.tracer_provider.shutdown()


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        yield client


# Фикстура приложения настраивает логи до capsys: перенастраиваем вывод на перехват.
def capture_logs() -> None:
    from user.observability import configure_logging

    configure_logging("INFO")


def events_from(output: str) -> list[dict]:
    return [
        json.loads(line)
        for line in output.splitlines()
        if line.startswith("{") and not json.loads(line)["message"].startswith("HTTP")
    ]


async def test_unhandled_error_is_500_with_request_id_header_and_log(
    client, capsys: pytest.CaptureFixture[str]
) -> None:
    capture_logs()
    response = await client.get("/boom", headers={"X-Request-ID": "rid-500"})
    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "rid-500"
    assert response.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "Internal server error",
            "details": {},
        }
    }
    output = capsys.readouterr().out
    assert "secret-text" not in output and "Exception in ASGI application" not in output
    events = events_from(output)
    failure = next(e for e in events if e["message"] == "unhandled_error")
    assert failure["request_id"] == "rid-500"
    assert failure["exception"]["type"] == "RuntimeError"
    assert any("boom" in frame for frame in failure["exception"]["frames"])
    access = next(e for e in events if e["message"] == "http_request")
    assert (access["request_id"], access["status_code"]) == ("rid-500", 500)
    assert access["path"] == "/boom"


async def test_docs_routes_are_logged_with_their_path(
    client, capsys: pytest.CaptureFixture[str]
) -> None:
    capture_logs()
    await client.get("/openapi.json")
    access = next(
        e
        for e in events_from(capsys.readouterr().out)
        if e["message"] == "http_request"
    )
    assert access["path"] == "/openapi.json"


async def test_metrics_include_runtime_and_process_collectors(client) -> None:
    text = (await client.get("/metrics")).text
    assert "python_info{" in text
    assert "python_gc_objects_collected_total" in text
    # ProcessCollector читает /proc и отдаёт метрики только на Linux (в контейнере).
    if sys.platform.startswith("linux"):
        assert "process_resident_memory_bytes" in text


async def test_probes_are_logged_at_debug_and_not_at_info(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from user.main import create_app

    async def hit(level: str) -> list[dict]:
        app = create_app(unavailable_db_settings(log_level=level))
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.get("/metrics")
                await client.get("/openapi.json")
            return events_from(capsys.readouterr().out)
        finally:
            await app.state.engine.dispose()
            app.state.tracer_provider.shutdown()

    info = [e["path"] for e in await hit("INFO") if e["message"] == "http_request"]
    assert info == ["/openapi.json"]
    debug = [e for e in await hit("DEBUG") if e["message"] == "http_request"]
    assert {(e["path"], e["level"]) for e in debug} == {
        ("/metrics", "debug"),
        ("/openapi.json", "info"),
    }


async def test_failed_health_check_is_still_logged_at_info(
    client, capsys: pytest.CaptureFixture[str]
) -> None:
    capture_logs()
    assert (await client.get("/health")).status_code == 503
    access = next(
        e
        for e in events_from(capsys.readouterr().out)
        if e["message"] == "http_request"
    )
    assert (access["level"], access["status_code"]) == ("info", 503)


async def test_probes_do_not_create_spans(app, client) -> None:
    exporter = InMemorySpanExporter()
    app.state.tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    await client.get("/metrics")
    await client.get("/health")
    assert exporter.get_finished_spans() == ()
    await client.get("/items/1")
    assert any(span.name.startswith("GET") for span in exporter.get_finished_spans())


async def test_schema_has_no_framework_validation_errors(client) -> None:
    document = (await client.get("/openapi.json")).json()
    operations = [
        operation for path in document["paths"].values() for operation in path.values()
    ]
    assert operations
    for operation in operations:
        assert "422" not in operation["responses"]
    schemas = document["components"]["schemas"]
    assert "HTTPValidationError" not in schemas and "ValidationError" not in schemas
    # Служебный 503 /health остаётся в едином формате с кодом INTERNAL_ERROR.
    health = document["paths"]["/health"]["get"]["responses"]["503"]
    assert health["content"]["application/json"]["example"]["error"]["code"] == (
        "INTERNAL_ERROR"
    )


async def test_validation_error_reports_only_type_and_location(client) -> None:
    response = await client.post("/payload", json={"count": "private-user-text"})
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["message"] == "Request validation failed"
    assert body["error"]["details"]["fields"] == [
        {"loc": ["body", "count"], "type": "int_parsing"}
    ]
    assert "private-user-text" not in response.text

    response = await client.get("/items/not-a-number?limit=also-private")
    assert response.status_code == 400
    assert "also-private" not in response.text and "not-a-number" not in response.text
    assert {"loc": ["path", "item_id"], "type": "int_parsing"} in (
        response.json()["error"]["details"]["fields"]
    )


async def test_validation_error_for_missing_and_nested_fields(client) -> None:
    missing = await client.post("/payload", json={})
    assert missing.json()["error"]["details"]["fields"] == [
        {"loc": ["body", "count"], "type": "missing"}
    ]
    nested = await client.post("/nested", json={"inner": {"values": [1, "x"]}})
    assert nested.json()["error"]["details"]["fields"] == [
        {"loc": ["body", "inner", "values", "1"], "type": "int_parsing"}
    ]
    assert "x" not in str(nested.json()["error"]["details"]["fields"][0]["loc"][:3])


async def test_malformed_json_reports_only_the_body(client) -> None:
    response = await client.post(
        "/payload", content="{bad", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["details"]["fields"] == [
        {"loc": ["body"], "type": "json_invalid"}
    ]


async def test_error_body_schema_documents_validation_details(client) -> None:
    schema = (await client.get("/openapi.json")).json()["components"]["schemas"]
    details = schema["ErrorBody"]["properties"]["details"]["description"]
    assert '"loc"' in details and '"type"' in details


@pytest.mark.parametrize("query", ["", "?x=1", "?token=private"])
async def test_probes_with_query_create_no_spans(app, client, query) -> None:
    exporter = InMemorySpanExporter()
    app.state.tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    await client.get(f"/health{query}")
    await client.get(f"/metrics{query}")
    assert exporter.get_finished_spans() == ()


async def test_client_disconnect_is_not_logged_as_unhandled_error(
    app, client, capsys: pytest.CaptureFixture[str]
) -> None:
    capture_logs()

    @app.get("/gone")
    async def gone() -> None:
        raise RuntimeError("No response returned.")

    response = await client.get("/gone")
    assert response.status_code == 499
    events = events_from(capsys.readouterr().out)
    assert not [e for e in events if e["message"] == "unhandled_error"]
    access = next(e for e in events if e["message"] == "http_request")
    assert (access["path"], access["status_code"]) == ("/gone", 499)


async def test_other_runtime_errors_are_still_unhandled(
    client, capsys: pytest.CaptureFixture[str]
) -> None:
    capture_logs()
    response = await client.get("/boom")
    assert response.status_code == 500
    assert any(
        e["message"] == "unhandled_error" for e in events_from(capsys.readouterr().out)
    )
