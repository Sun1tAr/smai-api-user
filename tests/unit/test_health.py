import httpx
import pytest
from fakes import unavailable_db_settings
from sqlalchemy import text


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


class FakeConnection:
    async def execute(self, statement) -> None:
        self.statement = str(statement)


class FakeEngine:
    def __init__(self) -> None:
        self.connection = FakeConnection()

    def connect(self):
        connection = self.connection

        class Context:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *exc) -> None:
                return None

        return Context()


async def test_health_returns_ok_when_database_answers() -> None:
    from user.main import create_app

    app = create_app(unavailable_db_settings())
    # Подменяем только движок: проверяется ответ приложения, а не сеть.
    fake = FakeEngine()
    real_engine = app.state.engine
    app.state.engine = fake
    try:
        async with client_for(app) as client:
            response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert fake.connection.statement == str(text("SELECT 1"))
    finally:
        await real_engine.dispose()
        app.state.tracer_provider.shutdown()


async def test_unavailable_database_returns_safe_503() -> None:
    from user.main import create_app

    app = create_app(unavailable_db_settings())
    try:
        async with client_for(app) as client:
            response = await client.get("/health")
        assert response.status_code == 503
        assert response.json() == {
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "Service is unavailable",
                "details": {},
            }
        }
        assert "private-test-password" not in response.text
    finally:
        await app.state.engine.dispose()
        app.state.tracer_provider.shutdown()


async def test_startup_fails_when_database_is_unavailable() -> None:
    from user.main import create_app

    app = create_app(unavailable_db_settings())
    with pytest.raises(RuntimeError, match="Service dependencies are unavailable"):
        async with app.router.lifespan_context(app):
            pytest.fail("Startup accepted an unavailable database")


async def test_error_format_for_404_405_and_unhandled_errors() -> None:
    from user.main import create_app

    app = create_app(unavailable_db_settings())

    @app.get("/boom")
    async def boom() -> None:
        raise ValueError("private-internal-detail")

    @app.get("/items/{item_id}")
    async def item(item_id: int) -> dict[str, int]:
        return {"id": item_id}

    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            not_found = await c.get("/nope")
            not_allowed = await c.post("/health")
            invalid = await c.get("/items/abc")
            crashed = await c.get("/boom")
        assert not_found.status_code == 404
        assert not_found.json() == {
            "error": {
                "code": "NOT_FOUND",
                "message": "Resource not found",
                "details": {},
            }
        }
        assert not_allowed.status_code == 405
        assert not_allowed.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
        assert invalid.status_code == 400
        error = invalid.json()["error"]
        assert error["code"] == "VALIDATION_ERROR"
        assert error["details"]["fields"][0]["loc"] == ["path", "item_id"]
        assert "abc" not in invalid.text
        assert crashed.status_code == 500
        assert crashed.json() == {
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "Internal server error",
                "details": {},
            }
        }
        assert "private-internal-detail" not in crashed.text
    finally:
        await app.state.engine.dispose()
        app.state.tracer_provider.shutdown()
