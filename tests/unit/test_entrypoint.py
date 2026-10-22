import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("migration_exit", [0, 9])
def test_entrypoint_starts_server_only_after_successful_migration(
    tmp_path: Path,
    migration_exit: int,
) -> None:
    entrypoint = Path(__file__).resolve().parents[2] / "docker" / "entrypoint.sh"
    migration_args = tmp_path / "migration-args"
    server_marker = tmp_path / "server-started"
    alembic = tmp_path / "alembic"
    alembic.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" > "$MIGRATION_ARGS_FILE"\n'
        f"exit {migration_exit}\n"
    )
    alembic.chmod(0o755)
    result = subprocess.run(
        [
            "sh",
            str(entrypoint),
            sys.executable,
            "-c",
            "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('started')",
            str(server_marker),
        ],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "MIGRATION_ARGS_FILE": str(migration_args),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert migration_args.read_text().strip() == "upgrade head"
    assert result.returncode == migration_exit
    assert server_marker.exists() is (migration_exit == 0)
