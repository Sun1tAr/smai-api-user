import pytest
from fakes import unavailable_db_settings


@pytest.fixture
def engine_kwargs(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    import user.database as database

    calls: list[dict] = []
    monkeypatch.setattr(
        database, "create_async_engine", lambda url, **kwargs: calls.append(kwargs)
    )
    return calls


def test_application_engine_limits_connections_and_statements(engine_kwargs) -> None:
    from user.database import create_engine

    create_engine(unavailable_db_settings(db_timeout_seconds=3))
    # lock_timeout относится только к миграциям: запросы приложения не меняются.
    assert engine_kwargs[0]["connect_args"] == {"timeout": 3, "command_timeout": 3}


def test_migration_lock_timeout_is_configurable(engine_kwargs) -> None:
    from user.database import create_migration_engine

    create_migration_engine(unavailable_db_settings(migration_lock_timeout_seconds=1.5))
    settings = engine_kwargs[0]["connect_args"]["server_settings"]
    assert settings == {"lock_timeout": "1500ms"}


def test_migration_engine_limits_only_the_connection(engine_kwargs) -> None:
    from user.database import create_migration_engine

    # DDL на большой таблице может идти дольше таймаута запросов приложения.
    create_migration_engine(unavailable_db_settings(db_timeout_seconds=3))
    assert engine_kwargs[0]["connect_args"] == {
        "timeout": 3,
        "server_settings": {"lock_timeout": "30000ms"},
    }
    assert engine_kwargs[0]["hide_parameters"] is True
