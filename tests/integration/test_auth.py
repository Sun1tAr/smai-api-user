import asyncio
import os

import httpx
import pytest
from sqlalchemy import text

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]

PASSWORD = "correct-horse-battery"


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


def credentials(login: str = "user@example.com", password: str = PASSWORD) -> dict:
    return {"login": login, "password": password}


async def test_register_then_login_on_postgresql(client, app) -> None:
    registered = await client.post("/register", json=credentials(" User@Example.com"))
    assert (registered.status_code, registered.json()) == (201, {"userId": 1})
    logged_in = await client.post("/login", json=credentials("USER@example.COM"))
    assert (logged_in.status_code, logged_in.json()) == (200, {"userId": 1})
    second = await client.post("/register", json=credentials("second@example.com"))
    assert second.json() == {"userId": 2}
    async with app.state.engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT login, password_hash, created_at FROM users WHERE id = 1")
            )
        ).one()
    assert row.login == "user@example.com"
    assert (
        row.password_hash.startswith("$argon2id$") and PASSWORD not in row.password_hash
    )
    assert row.created_at is not None and row.created_at.tzinfo is not None


async def test_duplicate_login_is_409_in_any_case(client) -> None:
    assert (await client.post("/register", json=credentials())).status_code == 201
    for login in ("user@example.com", "USER@Example.com"):
        response = await client.post("/register", json=credentials(login))
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "LOGIN_ALREADY_EXISTS"


async def test_concurrent_registration_of_one_login_creates_one_user(
    client, app
) -> None:
    responses = await asyncio.gather(
        *(client.post("/register", json=credentials()) for _ in range(4))
    )
    assert sorted(r.status_code for r in responses) == [201, 409, 409, 409]
    async with app.state.engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM users")) == 1


async def test_wrong_password_and_unknown_login_are_the_same_401(client) -> None:
    await client.post("/register", json=credentials())
    wrong = await client.post("/login", json=credentials(password="wrong-password-1"))
    unknown = await client.post("/login", json=credentials("nobody@example.com"))
    assert (wrong.status_code, unknown.status_code) == (401, 401)
    assert wrong.json() == unknown.json()


async def test_users_table_matches_the_agreed_schema(app) -> None:
    async with app.state.engine.connect() as connection:
        columns = {
            row.column_name: row
            for row in await connection.execute(
                text(
                    "SELECT column_name, data_type, is_nullable, is_identity, "
                    "column_default FROM information_schema.columns "
                    "WHERE table_name = 'users'"
                )
            )
        }
        constraints = {
            row.conname: row.contype.decode()
            if isinstance(row.contype, bytes)
            else row.contype
            for row in await connection.execute(
                text(
                    "SELECT conname, contype::text AS contype FROM pg_constraint "
                    "WHERE conrelid = 'users'::regclass"
                )
            )
        }
    assert set(columns) == {"id", "login", "password_hash", "created_at"}
    assert columns["id"].data_type == "integer" and columns["id"].is_identity == "YES"
    assert columns["login"].data_type == "text"
    assert columns["password_hash"].data_type == "text"
    assert columns["created_at"].data_type == "timestamp with time zone"
    assert columns["created_at"].column_default == "now()"
    assert all(column.is_nullable == "NO" for column in columns.values())
    # Только PK и UNIQUE(login): ни внешних ключей, ни лишних CHECK.
    assert constraints == {"pk_users": "p", "uq_users_login": "u"}
