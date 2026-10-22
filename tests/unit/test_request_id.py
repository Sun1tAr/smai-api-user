import httpx
import pytest
from fakes import unavailable_db_settings


@pytest.fixture
async def client():
    from user.main import create_app

    app = create_app(unavailable_db_settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client
    await app.state.engine.dispose()
    app.state.tracer_provider.shutdown()


@pytest.mark.parametrize("request_id", ["abc-123", "a.b_c-D", "x" * 128])
async def test_valid_incoming_request_id_is_accepted(
    client: httpx.AsyncClient, request_id: str
) -> None:
    response = await client.get("/health", headers={"X-Request-ID": request_id})
    assert response.headers["X-Request-ID"] == request_id


@pytest.mark.parametrize("request_id", ["x" * 129, "bad id", "id;drop", "a/b", ""])
async def test_invalid_incoming_request_id_is_replaced(
    client: httpx.AsyncClient, request_id: str
) -> None:
    import uuid

    response = await client.get("/health", headers={"X-Request-ID": request_id})
    generated = response.headers["X-Request-ID"]
    assert generated != request_id
    assert str(uuid.UUID(generated)) == generated


async def test_missing_request_id_is_generated(client: httpx.AsyncClient) -> None:
    import uuid

    response = await client.get("/health")
    uuid.UUID(response.headers["X-Request-ID"])


# Приложение создаётся после capsys: обработчик логов привязывается к stdout.
async def test_request_id_appears_in_log(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    from user.main import create_app

    app = create_app(unavailable_db_settings())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get(
                "/health?token=private-query", headers={"X-Request-ID": "rid-1"}
            )
        # Строки клиента httpx (с query) не относятся к сервису, оставляем его события.
        entries = [
            entry
            for entry in map(json.loads, capsys.readouterr().out.splitlines())
            if not entry["message"].startswith("HTTP Request")
        ]
        request_log = next(i for i in entries if i["message"] == "http_request")
        assert request_log["request_id"] == "rid-1"
        assert request_log["path"] == "/health"
        assert "private-query" not in json.dumps(entries)
    finally:
        await app.state.engine.dispose()
        app.state.tracer_provider.shutdown()
