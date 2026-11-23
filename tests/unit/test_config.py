from pathlib import Path

import pytest
from pydantic import ValidationError


def required_settings(**extra: object) -> dict[str, object]:
    return {
        "db_host": "postgres",
        "db_name": "user",
        "db_user": "user",
        "db_password": "local-only",
        **extra,
    }


def make(**extra: object):
    from user.config import Settings

    return Settings(_env_file=None, **required_settings(**extra))


def test_missing_required_settings_prevent_startup() -> None:
    from user.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize("missing", ["db_host", "db_name", "db_user", "db_password"])
def test_each_required_setting_is_required(missing: str) -> None:
    from user.config import Settings

    values = required_settings()
    del values[missing]
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, **values)
    assert missing in str(error.value)


def test_defaults() -> None:
    settings = make()
    assert settings.db_port == 5432
    assert settings.db_timeout_seconds == 5
    assert settings.log_level == "INFO"
    assert settings.otel_exporter_otlp_endpoint is None
    assert settings.migration_lock_timeout_seconds == 30


def test_environment_overrides_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    from user.config import Settings

    Path(".env").write_text(
        "DB_HOST=postgres\nDB_NAME=user\nDB_USER=user\nDB_PASSWORD=local-only\n"
    )
    monkeypatch.setenv("DB_HOST", "deployment-db")
    settings = Settings()
    assert settings.db_host == "deployment-db"
    assert settings.db_name == "user"


def test_extra_environment_is_ignored() -> None:
    from user.config import Settings

    Path(".env").write_text(
        "DB_HOST=postgres\nDB_NAME=user\nDB_USER=user\n"
        "DB_PASSWORD=local-only\n"
        "USER_HTTP_PORT=8002\nUNKNOWN=1\n"
    )
    assert Settings().db_host == "postgres"


def test_empty_values_in_dotenv_mean_unset() -> None:
    from user.config import Settings

    Path(".env").write_text(
        "DB_HOST=postgres\nDB_NAME=user\nDB_USER=user\n"
        "DB_PASSWORD=local-only\n"
        "DB_PORT=\nDB_TIMEOUT_SECONDS=\nLOG_LEVEL=\nOTEL_EXPORTER_OTLP_ENDPOINT=\n"
        "MIGRATION_LOCK_TIMEOUT_SECONDS=\n"
    )
    settings = Settings()
    assert settings.db_port == 5432
    assert settings.db_timeout_seconds == 5
    assert settings.log_level == "INFO"
    assert settings.otel_exporter_otlp_endpoint is None
    assert settings.migration_lock_timeout_seconds == 30


def test_empty_required_environment_value_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from user.config import Settings

    monkeypatch.setenv("DB_HOST", "postgres")
    monkeypatch.setenv("DB_NAME", "user")
    monkeypatch.setenv("DB_USER", "user")
    monkeypatch.setenv("DB_PASSWORD", "")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_values_are_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from user.config import Settings

    for key, value in {
        "DB_HOST": "postgres",
        "DB_NAME": "user",
        "DB_USER": "user",
        "DB_PASSWORD": "local-only",
        "LOG_LEVEL": "WARNING",
        "MIGRATION_LOCK_TIMEOUT_SECONDS": "12",
    }.items():
        monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None)
    assert settings.log_level == "WARNING"
    assert settings.migration_lock_timeout_seconds == 12


def test_log_level_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert make().log_level == "DEBUG"


@pytest.mark.parametrize(
    "override",
    [
        {"db_port": 0},
        {"db_port": 65536},
        {"db_timeout_seconds": 0},
        {"db_timeout_seconds": 61},
        {"log_level": "TRACE"},
        {"migration_lock_timeout_seconds": 0},
        {"migration_lock_timeout_seconds": 3601},
    ],
)
def test_out_of_range_values_are_rejected(override: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make(**override)


def test_database_url_preserves_special_characters_without_exposing_password() -> None:
    secret = "local@:/%?#password"
    settings = make(db_password=secret)
    assert settings.database_url.password == secret
    assert settings.database_url.drivername == "postgresql+asyncpg"
    assert secret not in repr(settings)
    assert secret not in str(settings.database_url)


def test_validation_error_does_not_echo_secret() -> None:
    secret = "test-secret-that-must-not-appear"
    with pytest.raises(ValidationError) as error:
        make(db_password=secret, db_port="invalid")
    assert secret not in str(error.value)


@pytest.mark.parametrize("field", ["db_host", "db_name", "db_user", "db_password"])
@pytest.mark.parametrize("blank", [" ", "   ", "\t\n"])
def test_blank_string_settings_are_rejected(field: str, blank: str) -> None:
    with pytest.raises(ValidationError) as error:
        make(**{field: blank})
    assert field in str(error.value)


def test_password_with_surrounding_spaces_is_kept_unchanged() -> None:
    assert make(db_password=" pass word ").db_password.get_secret_value() == (
        " pass word "
    )


@pytest.mark.parametrize(
    "url", ["http://user:private-pass@host:4318", "http://user@host:4318"]
)
def test_urls_with_credentials_are_rejected_without_echoing_them(url: str) -> None:
    with pytest.raises(ValidationError) as error:
        make(otel_exporter_otlp_endpoint=url)
    text = str(error.value)
    assert "credentials" in text
    assert "private-pass" not in text and "user" not in text.replace(
        "user:password", ""
    )


def test_hashing_limits_have_defaults_and_bounds(monkeypatch) -> None:
    from pydantic import ValidationError

    from user.config import Settings

    base = {"db_host": "h", "db_name": "n", "db_user": "u", "db_password": "p"}
    settings = Settings(_env_file=None, **base)
    assert (
        settings.hash_concurrency,
        settings.hash_queue_max,
        settings.hash_queue_timeout_seconds,
    ) == (4, 32, 5)
    for field, value in (
        ("hash_concurrency", 0),
        ("hash_queue_max", -1),
        ("hash_queue_timeout_seconds", 0),
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **base, **{field: value})
    monkeypatch.setenv("HASH_CONCURRENCY", "2")
    assert Settings(_env_file=None, **base).hash_concurrency == 2
