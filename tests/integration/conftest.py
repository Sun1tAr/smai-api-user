import os

import pytest

from user.config import Settings


# Интеграционные тесты подключаются только к отдельной тестовой БД.
@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        db_host=os.environ["TEST_DB_HOST"],
        db_port=int(os.getenv("TEST_DB_PORT", "5432")),
        db_name=os.environ["TEST_DB_NAME"],
        db_user=os.environ["TEST_DB_USER"],
        db_password=os.environ["TEST_DB_PASSWORD"],
    )


# Alembic читает конфигурацию из окружения, как при запуске контейнера.
@pytest.fixture
def alembic_env(settings: Settings) -> dict[str, str]:
    return {
        **os.environ,
        "DB_HOST": settings.db_host,
        "DB_PORT": str(settings.db_port),
        "DB_NAME": settings.db_name,
        "DB_USER": settings.db_user,
        "DB_PASSWORD": settings.db_password.get_secret_value(),
    }


# Схема создаётся настоящей миграцией, как при старте контейнера; таблица users
# очищается перед каждым тестом.
@pytest.fixture
async def app(settings: Settings, alembic_env: dict[str, str]):
    import subprocess

    from sqlalchemy import text

    from user.main import create_app

    result = subprocess.run(
        ["alembic", "upgrade", "head"], env=alembic_env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with app.state.engine.begin() as connection:
            await connection.execute(text("TRUNCATE users RESTART IDENTITY"))
        yield app
