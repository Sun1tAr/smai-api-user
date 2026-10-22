from typing import Any

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from user.config import Settings

# Общий PostgreSQL на стенде: у сервиса своя таблица версий Alembic.
ALEMBIC_VERSION_TABLE = "alembic_version_user"


# Явные имена ограничений делают миграции воспроизводимыми и позволяют их распознавать.
class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


# Ограничиваем ожидание БД, чтобы проверка готовности не зависала.
def create_engine(settings: Settings) -> AsyncEngine:
    return _create_engine(settings, command_timeout=settings.db_timeout_seconds)


# Миграции не ограничиваются таймаутом запросов приложения: DDL на большой таблице
# идёт дольше. Ожидание подключения ограничено так же, как у приложения, а ожидание
# чужой блокировки — lock_timeout.
def create_migration_engine(settings: Settings) -> AsyncEngine:
    lock_timeout_ms = round(settings.migration_lock_timeout_seconds * 1000)
    return _create_engine(
        settings,
        command_timeout=None,
        server_settings={"lock_timeout": f"{lock_timeout_ms}ms"},
    )


def _create_engine(
    settings: Settings,
    *,
    command_timeout: float | None,
    server_settings: dict[str, str] | None = None,
) -> AsyncEngine:
    connect_args: dict[str, Any] = {"timeout": settings.db_timeout_seconds}
    if command_timeout is not None:
        connect_args["command_timeout"] = command_timeout
    if server_settings:
        connect_args["server_settings"] = server_settings
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_timeout=settings.db_timeout_seconds,
        hide_parameters=True,
        connect_args=connect_args,
    )
