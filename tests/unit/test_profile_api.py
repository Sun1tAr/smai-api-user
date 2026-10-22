from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest
from fakes import unavailable_db_settings

from user import api
from user.repository import Profile

CREATED = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)


class FakeProfiles:
    """Заменяет репозиторий: unit-тесты не ходят в БД."""

    def __init__(self) -> None:
        self.rows = {42: Profile(42, "user@example.com", CREATED)}
        self.fail: Exception | None = None
        self.calls: list[int] = []

    async def find(self, session_factory, user_id: int) -> Profile | None:
        self.calls.append(user_id)
        if self.fail:
            raise self.fail
        return self.rows.get(user_id)


@pytest.fixture
async def app(monkeypatch: pytest.MonkeyPatch):
    from user.main import create_app

    app = create_app(unavailable_db_settings())
    profiles = FakeProfiles()
    monkeypatch.setattr(api, "find_profile", profiles.find)
    app.state.profiles = profiles
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


def error_code(response: httpx.Response) -> str:
    return response.json()["error"]["code"]


async def test_profile_returns_user_data(client) -> None:
    response = await client.get("/profile", headers={"X-User-Id": "42"})
    assert response.status_code == 200
    assert response.json() == {
        "userId": 42,
        "login": "user@example.com",
        "createdAt": "2026-10-14T12:00:00Z",
    }


async def test_created_at_is_utc_with_z_and_without_fraction(client, app) -> None:
    moscow = timezone(timedelta(hours=3))
    app.state.profiles.rows[7] = Profile(
        7, "a@b.ru", datetime(2026, 10, 14, 15, 0, 0, 123456, tzinfo=moscow)
    )
    response = await client.get("/profile", headers={"X-User-Id": "7"})
    assert response.json()["createdAt"] == "2026-10-14T12:00:00Z"


async def test_missing_header_is_401_without_db_query(client, app) -> None:
    response = await client.get("/profile")
    assert response.status_code == 401
    assert error_code(response) == "UNAUTHORIZED"
    assert app.state.profiles.calls == []


@pytest.mark.parametrize(
    "value",
    ["", "0", "-1", "abc", "4 2", " 42", "42 ", "4.2", "+42", "042", "٤٢", "1e3"]
    + ["2147483648", "99999999999999999999"],
)
async def test_bad_header_is_401_without_db_query(client, app, value) -> None:
    response = await client.get("/profile", headers={"X-User-Id": value.encode()})
    assert response.status_code == 401
    assert error_code(response) == "UNAUTHORIZED"
    assert app.state.profiles.calls == []


async def test_duplicated_header_is_401(client, app) -> None:
    response = await client.get(
        "/profile", headers=[("X-User-Id", "42"), ("x-user-id", "43")]
    )
    assert response.status_code == 401
    assert app.state.profiles.calls == []


async def test_max_int_id_is_looked_up(client, app) -> None:
    response = await client.get("/profile", headers={"X-User-Id": "2147483647"})
    assert response.status_code == 404
    assert app.state.profiles.calls == [2147483647]


async def test_unknown_user_is_404(client) -> None:
    response = await client.get("/profile", headers={"X-User-Id": "43"})
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "USER_NOT_FOUND", "message": "User not found", "details": {}}
    }


async def test_database_failure_is_500_without_details(client, app) -> None:
    app.state.profiles.fail = RuntimeError("password=private-test-password")
    response = await client.get("/profile", headers={"X-User-Id": "42"})
    assert response.status_code == 500
    assert error_code(response) == "INTERNAL_ERROR"
    assert "private-test-password" not in response.text


async def test_response_has_no_password_hash(client) -> None:
    response = await client.get("/profile", headers={"X-User-Id": "42"})
    assert set(response.json()) == {"userId", "login", "createdAt"}


async def test_post_profile_is_405(client) -> None:
    response = await client.post("/profile", headers={"X-User-Id": "42"})
    assert response.status_code == 405


async def test_openapi_documents_profile(client) -> None:
    schema = (await client.get("/openapi.json")).json()
    operation = schema["paths"]["/profile"]["get"]
    assert operation["summary"] and operation["description"]
    assert set(operation["responses"]) == {"200", "401", "404", "500"}
    assert operation["parameters"][0]["name"] == "X-User-Id"
    for status in ("200", "401", "404", "500"):
        media = operation["responses"][status]["content"]["application/json"]
        assert "example" in media or "examples" in media or "schema" in media
    profile = schema["components"]["schemas"]["ProfileResponse"]
    assert profile["example"]["createdAt"] == "2026-10-14T12:00:00Z"
