"""Create users table (SMAI-100)

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# LE-11. Ограничения названы явно: сервис распознаёт дубликат логина по uq_users_login.
def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), sa.Identity(), nullable=False),
        sa.Column("login", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("login", name="uq_users_login"),
    )


def downgrade() -> None:
    op.drop_table("users")
