import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _stage(name: str) -> str:
    dockerfile = (ROOT / "Dockerfile").read_text()
    match = re.search(
        rf"^FROM .* AS {name}$(.*?)(?=^FROM |\Z)", dockerfile, re.MULTILINE | re.DOTALL
    )
    assert match is not None
    return match.group(1)


def test_runtime_starts_application_as_unprivileged_user() -> None:
    runtime = _stage("runtime")
    commands = re.findall(r"^CMD (\[.*\])$", runtime, re.MULTILINE)
    assert json.loads(commands[-1]) == ["python", "-m", "user"]
    assert "--uid 10001" in runtime
    assert re.search(r"^USER user$", runtime, re.MULTILINE)
    assert "EXPOSE 8000" in runtime
    assert "HEALTHCHECK" in runtime and "/health" in runtime


def test_production_stage_exists() -> None:
    assert re.search(
        r"^FROM runtime AS production$", (ROOT / "Dockerfile").read_text(), re.MULTILINE
    )


def test_compose_service_is_named_user_and_does_not_publish_port() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    assert re.search(r"^  user:$", compose, re.MULTILINE)
    # Доступ только по сети smai: порт на хост публикует лишь dev-override.
    assert "ports:" not in compose
    dev = (ROOT / "docker-compose.dev.yml").read_text()
    assert '"127.0.0.1:${USER_HTTP_PORT:-8003}:8000"' in dev
    # Gateway находит сервис по имени user в общей внешней сети smai.
    assert re.search(r"^  smai:\n    external: true$", compose, re.MULTILINE)
    assert "- user" in compose
    assert "condition: service_healthy" in compose
    assert "postgres:17-alpine" in compose


def test_compose_requires_database_secrets() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    for name in ("DB_NAME", "DB_USER", "DB_PASSWORD"):
        assert f"${{{name}:?" in compose
