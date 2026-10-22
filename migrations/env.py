import asyncio

import structlog
from alembic import context
from asyncpg import PostgresError
from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

from user import models  # noqa: F401  (регистрирует таблицы в Base.metadata)
from user.config import Settings
from user.database import ALEMBIC_VERSION_TABLE, Base, create_migration_engine
from user.migration_diagnostics import describe_migration_error
from user.observability import configure_logging

settings = Settings()
configure_logging(settings.log_level)
target_metadata = Base.metadata
# Ревизии для диагностики падения: с какой начали и какая применена последней.
progress: dict[str, str | None] = {"from_revision": None, "last_applied": None}


# Offline-режим генерирует SQL без подключения к PostgreSQL.
def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        version_table=ALEMBIC_VERSION_TABLE,
    )
    with context.begin_transaction():
        context.run_migrations()


# Alembic выполняет синхронные операции через async-соединение SQLAlchemy.
def migrate(connection: Connection) -> None:
    def record_applied(*, ctx, step, heads, run_args) -> None:
        progress["last_applied"] = step.up_revision_id

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        version_table=ALEMBIC_VERSION_TABLE,
        on_version_apply=record_applied,
    )
    progress["from_revision"] = context.get_context().get_current_revision()
    with context.begin_transaction():
        context.run_migrations()


# Подключение миграций берёт настройки приложения, но без таймаута запросов.
async def run_migrations_online() -> None:
    engine = create_migration_engine(settings)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(migrate)
    except (OSError, PostgresError, SQLAlchemyError) as error:
        structlog.get_logger().error(
            "migration_failed", **describe_migration_error(error), **progress
        )
        raise SystemExit(1) from None
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
