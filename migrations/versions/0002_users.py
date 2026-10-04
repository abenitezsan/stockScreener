"""cuentas de usuario: email, sesiones, filtros guardados y seguimiento por usuario

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-04 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("(CURRENT_TIMESTAMP)")


def _watchlist(with_user: bool) -> None:
    columns = [
        sa.Column("security_id", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("margin_of_safety", sa.Double(), nullable=True),
        sa.Column("target_total_return", sa.Double(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["securities.id"],
            name=op.f("fk_watchlist_security_id_securities"),
            ondelete="CASCADE",
        ),
    ]
    if with_user:
        columns = [
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=True),
            *columns,
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["users.id"],
                name=op.f("fk_watchlist_user_id_users"),
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_watchlist")),
            sa.UniqueConstraint("user_id", "security_id", name=op.f("uq_watchlist_user_id")),
        ]
    else:
        columns.append(sa.PrimaryKeyConstraint("security_id", name=op.f("pk_watchlist")))
    op.create_table("watchlist", *columns)


def upgrade() -> None:
    # `users` no la usaba nada todavía: se recrea con email en lugar de username
    op.drop_table("users")
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_table(
        "user_sessions",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("token_hash", name=op.f("pk_user_sessions")),
    )
    with op.batch_alter_table("user_sessions") as batch_op:
        batch_op.create_index(batch_op.f("ix_user_sessions_user_id"), ["user_id"], unique=False)
    op.create_table(
        "saved_filters",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_saved_filters_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saved_filters")),
        sa.UniqueConstraint("user_id", "name", name=op.f("uq_saved_filters_user_id")),
    )

    # Seguimiento por usuario. Las filas existentes quedan con user_id NULL.
    op.rename_table("watchlist", "watchlist_old")
    _watchlist(with_user=True)
    op.execute(
        "INSERT INTO watchlist (security_id, notes, margin_of_safety, target_total_return, added_at)"
        " SELECT security_id, notes, margin_of_safety, target_total_return, added_at"
        " FROM watchlist_old"
    )
    op.drop_table("watchlist_old")


def downgrade() -> None:
    op.rename_table("watchlist", "watchlist_new")
    _watchlist(with_user=False)
    # Si dos usuarios seguían el mismo valor, se conserva una sola fila
    op.execute(
        "INSERT INTO watchlist (security_id, notes, margin_of_safety, target_total_return, added_at)"
        " SELECT security_id, notes, margin_of_safety, target_total_return, added_at"
        " FROM watchlist_new WHERE id IN (SELECT MIN(id) FROM watchlist_new GROUP BY security_id)"
    )
    op.drop_table("watchlist_new")
    op.drop_table("saved_filters")
    with op.batch_alter_table("user_sessions") as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_sessions_user_id"))
    op.drop_table("user_sessions")
    op.drop_table("users")
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("username", name=op.f("uq_users_username")),
    )
