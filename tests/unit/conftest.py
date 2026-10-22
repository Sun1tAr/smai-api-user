from pathlib import Path

import pytest


# Ни .env разработчика, ни переменные процесса не должны влиять на unit-тесты.
@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for key in (
        "DB_HOST",
        "DB_PORT",
        "DB_NAME",
        "DB_USER",
        "DB_PASSWORD",
        "DB_TIMEOUT_SECONDS",
        "MIGRATION_LOCK_TIMEOUT_SECONDS",
        "HASH_CONCURRENCY",
        "HASH_QUEUE_MAX",
        "HASH_QUEUE_TIMEOUT_SECONDS",
        "LOG_LEVEL",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    ):
        monkeypatch.delenv(key, raising=False)
