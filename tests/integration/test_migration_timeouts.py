import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]

ROOT = Path(__file__).resolve().parents[2]
REVISION = '''"""temporary"""
from alembic import op

revision = "tmp0001"
down_revision = None


def upgrade() -> None:
    op.execute({statement!r})


def downgrade() -> None:
    pass
'''


# Временная ветка миграций с настоящим env.py проекта и одной ревизией: реальные
# ревизии сервиса не копируются, иначе получится вторая голова.
def run_alembic(tmp_path: Path, env: dict[str, str], statement: str):
    shutil.copytree(
        ROOT / "migrations",
        tmp_path / "migrations",
        ignore=shutil.ignore_patterns("0*.py", "__pycache__"),
    )
    (tmp_path / "migrations" / "versions" / "tmp0001.py").write_text(
        REVISION.format(statement=statement)
    )
    (tmp_path / "alembic.ini").write_text(
        "[alembic]\nscript_location = %(here)s/migrations\n"
    )
    return subprocess.run(
        ["alembic", "-c", str(tmp_path / "alembic.ini"), "upgrade", "head"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def test_migration_longer_than_db_timeout_succeeds(
    tmp_path: Path, alembic_env: dict[str, str]
) -> None:
    env = {**alembic_env, "DB_TIMEOUT_SECONDS": "1"}
    passed = False
    try:
        result = run_alembic(tmp_path, env, "SELECT pg_sleep(2.5)")
        assert result.returncode == 0, result.stdout + result.stderr
        passed = True
    finally:
        # Откат обязателен: иначе tmp0001 останется в БД и сломает следующие тесты.
        cleanup = subprocess.run(
            ["alembic", "-c", str(tmp_path / "alembic.ini"), "downgrade", "base"],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        # Ошибка самого теста не подменяется ошибкой отката.
        if passed:
            assert cleanup.returncode == 0, cleanup.stdout + cleanup.stderr


def test_failed_migration_logs_sqlstate_and_revisions_without_secrets(
    tmp_path: Path, alembic_env: dict[str, str]
) -> None:
    result = run_alembic(tmp_path, alembic_env, "SELECT * FROM missing_table")
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert alembic_env["DB_PASSWORD"] not in output
    failure = next(
        item
        for item in map(json.loads, result.stdout.splitlines())
        if item["message"] == "migration_failed"
    )
    assert failure["sqlstate"] == "42P01"
    assert failure["from_revision"] is None
    assert failure["error_type"]


async def test_migration_waiting_for_a_foreign_lock_fails_by_lock_timeout(
    tmp_path: Path, alembic_env: dict[str, str], settings
) -> None:
    import asyncio

    import asyncpg

    connection = await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password.get_secret_value(),
        database=settings.db_name,
    )
    await connection.execute("CREATE TABLE IF NOT EXISTS lock_probe (id integer)")
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute("LOCK TABLE lock_probe IN ACCESS EXCLUSIVE MODE")
        env = {**alembic_env, "MIGRATION_LOCK_TIMEOUT_SECONDS": "1"}
        result = await asyncio.to_thread(
            run_alembic, tmp_path, env, "LOCK TABLE lock_probe"
        )
    finally:
        await transaction.rollback()
        await connection.execute("DROP TABLE IF EXISTS lock_probe")
        await connection.close()
    assert result.returncode != 0
    failure = next(
        item
        for item in map(json.loads, result.stdout.splitlines())
        if item["message"] == "migration_failed"
    )
    # 55P03 lock_not_available: ждали чужую блокировку не дольше lock_timeout.
    assert failure["sqlstate"] == "55P03"
