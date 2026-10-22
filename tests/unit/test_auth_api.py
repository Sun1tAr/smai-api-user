import httpx
import pytest
from asyncpg.exceptions import NotNullViolationError, UniqueViolationError
from fakes import unavailable_db_settings
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy.exc import IntegrityError

from user import api
from user.hashing import HashingBusy
from user.observability import configure_logging
from user.repository import Credentials, LoginAlreadyExists, create_user

PASSWORD = "correct-horse-battery"
LOGIN = "user@example.com"


class FakeUsers:
    """Заменяет репозиторий: unit-тесты не ходят в БД."""

    def __init__(self, passwords) -> None:
        self.passwords = passwords
        self.rows: dict[str, Credentials] = {}
        self.fail: Exception | None = None

    async def create(self, session_factory, login: str, password_hash: str) -> int:
        if self.fail:
            raise self.fail
        if login in self.rows:
            raise LoginAlreadyExists
        self.rows[login] = Credentials(len(self.rows) + 1, password_hash)
        return self.rows[login].id

    async def find(self, session_factory, login: str) -> Credentials | None:
        if self.fail:
            raise self.fail
        return self.rows.get(login)


@pytest.fixture
async def app(monkeypatch: pytest.MonkeyPatch):
    from user.main import create_app

    app = create_app(unavailable_db_settings())
    users = FakeUsers(app.state.passwords)
    monkeypatch.setattr(api, "create_user", users.create)
    monkeypatch.setattr(api, "find_credentials", users.find)
    app.state.users = users
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


def credentials(login: str = LOGIN, password: str = PASSWORD) -> dict:
    return {"login": login, "password": password}


async def test_register_returns_201_and_user_id(client, app) -> None:
    response = await client.post("/register", json=credentials("  User@Example.com "))
    assert response.status_code == 201
    assert response.json() == {"userId": 1}
    stored = app.state.users.rows[LOGIN]
    assert stored.password_hash.startswith("$argon2id$")
    assert PASSWORD not in stored.password_hash


async def test_register_duplicate_login_is_409_in_any_case(client) -> None:
    await client.post("/register", json=credentials())
    response = await client.post("/register", json=credentials("USER@example.com"))
    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "LOGIN_ALREADY_EXISTS",
            "message": "User with this login already exists",
            "details": {},
        }
    }


async def test_login_returns_200_and_user_id(client) -> None:
    await client.post("/register", json=credentials())
    response = await client.post("/login", json=credentials("USER@example.com"))
    assert response.status_code == 200
    assert response.json() == {"userId": 1}


async def test_wrong_password_and_unknown_login_give_identical_401(
    client, app, monkeypatch: pytest.MonkeyPatch
) -> None:
    await client.post("/register", json=credentials())
    calls: list[str] = []
    original = app.state.passwords.verify

    async def counting_verify(password_hash: str, password: str) -> bool:
        calls.append(password_hash)
        return await original(password_hash, password)

    monkeypatch.setattr(app.state.passwords, "verify", counting_verify)
    wrong = await client.post("/login", json=credentials(password="wrong-password-1"))
    unknown = await client.post("/login", json=credentials("nobody@example.com"))
    assert (wrong.status_code, unknown.status_code) == (401, 401)
    assert wrong.json() == unknown.json()
    assert wrong.json()["error"]["code"] == "INVALID_CREDENTIALS"
    # verify по разу на запрос; для неизвестного логина — по фиктивному хэшу
    assert calls == [
        app.state.users.rows[LOGIN].password_hash,
        app.state.passwords.dummy_hash,
    ]


@pytest.mark.parametrize(
    ("content", "headers"),
    [
        (b"", {"Content-Type": "application/json"}),
        (b"{not json", {"Content-Type": "application/json"}),
        (b'{"login": "a@x.ru"}', {"Content-Type": "application/json"}),
        (b'{"login": 1, "password": 2}', {"Content-Type": "application/json"}),
        (b'{"login": null, "password": null}', {"Content-Type": "application/json"}),
        (b"[]", {"Content-Type": "application/json"}),
        (b"login=a%40x.ru&password=whatever-password-1", {}),
        (
            b'{"login": "a@x.ru", "password": "whatever-password-1"}',
            {"Content-Type": "text/plain"},
        ),
    ],
)
@pytest.mark.parametrize("path", ["/register", "/login"])
async def test_malformed_requests_are_400_without_echoing_input(
    client, path: str, content: bytes, headers: dict
) -> None:
    response = await client.post(path, content=content, headers=headers)
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "whatever-password" not in response.text and "input" not in response.text


@pytest.mark.parametrize(
    "password", ["short", "x" * 129, "Password1234", "user@example.com-extra"]
)
async def test_register_rejects_weak_passwords_with_400(client, password: str) -> None:
    response = await client.post("/register", json=credentials(password=password))
    assert response.status_code == 400
    assert response.json()["error"]["details"]["fields"][0]["loc"] == [
        "body",
        "password",
    ]
    # "input" в ответе нет, значит нет и самого значения
    assert "input" not in response.text


async def test_validation_response_never_contains_the_password(client) -> None:
    secret = "private-password-value-x" * 8  # длиннее 128: 400
    response = await client.post("/register", json=credentials(password=secret))
    assert response.status_code == 400
    assert "private-password-value" not in response.text


async def test_busy_hashing_is_503_with_retry_after(client, app, monkeypatch) -> None:
    async def busy(*args: str) -> str:
        raise HashingBusy

    monkeypatch.setattr(app.state.passwords, "hash", busy)
    monkeypatch.setattr(app.state.passwords, "verify", busy)
    for path in ("/register", "/login"):
        response = await client.post(path, json=credentials())
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "5"
        assert response.json()["error"]["code"] == "SERVICE_BUSY"


@pytest.mark.parametrize("path", ["/register", "/login"])
async def test_database_failure_is_500_without_details(
    client, app, path: str, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("INFO")
    app.state.users.fail = RuntimeError("connect to 10.0.0.5 as dbuser failed")
    response = await client.post(path, json=credentials())
    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "Internal server error",
            "details": {},
        }
    }
    output = capsys.readouterr().out
    assert "10.0.0.5" not in output and PASSWORD not in output


async def test_password_is_absent_from_logs_and_spans(
    app, client, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("INFO")
    exporter = InMemorySpanExporter()
    app.state.tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    await client.post("/register", json=credentials())
    await client.post("/login", json=credentials(password="wrong-password-1"))
    await client.post("/register", json=credentials(password="short-pw-ZZ"))
    output = capsys.readouterr().out
    assert "http_request" in output
    for secret in (PASSWORD, "wrong-password-1", "short-pw-ZZ"):
        assert secret not in output
        assert secret not in repr(
            [
                (span.name, dict(span.attributes or {}))
                for span in exporter.get_finished_spans()
            ]
        )


async def test_openapi_documents_both_endpoints_and_every_error(client) -> None:
    schema = (await client.get("/openapi.json")).json()
    register = schema["paths"]["/register"]["post"]
    login = schema["paths"]["/login"]["post"]
    assert set(register["responses"]) == {"201", "400", "409", "500", "503"}
    assert set(login["responses"]) == {"200", "400", "401", "500", "503"}
    for operation in (register, login):
        assert operation["summary"] and operation["description"]
        for status, response in operation["responses"].items():
            if status[0] != "2":
                assert response["content"]["application/json"]["example"]["error"]
    assert "Retry-After" in register["responses"]["503"]["headers"]
    assert schema["components"]["schemas"]["UserIdResponse"]["example"] == {
        "userId": 42
    }


class FakeSession:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def begin(self) -> "FakeSession":
        return self

    def add(self, item: object) -> None:
        return None

    async def flush(self) -> None:
        raise self.error


def integrity_error(driver_error: Exception) -> IntegrityError:
    return IntegrityError("INSERT", {}, driver_error)


async def test_create_user_maps_only_the_login_unique_violation_to_duplicate() -> None:
    duplicate = UniqueViolationError.new(
        {"M": "dup", "C": "23505", "n": "uq_users_login"}
    )
    with pytest.raises(LoginAlreadyExists):
        await create_user(lambda: FakeSession(integrity_error(duplicate)), "a", "h")


@pytest.mark.parametrize(
    "driver_error",
    [
        UniqueViolationError.new({"M": "dup", "C": "23505", "n": "pk_users"}),
        UniqueViolationError.new({"M": "dup", "C": "23505"}),
        NotNullViolationError.new({"M": "null", "C": "23502", "n": "uq_users_login"}),
        ValueError("not a driver error"),
    ],
)
async def test_create_user_keeps_other_integrity_errors(driver_error) -> None:
    with pytest.raises(IntegrityError):
        await create_user(lambda: FakeSession(integrity_error(driver_error)), "a", "h")
