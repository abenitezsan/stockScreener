"""conector de Claude (MCP): OAuth, tokens, historial y análisis

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-02 19:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:

    op.create_table(
        "oauth_clients",
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("redirect_uris", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("client_id", name=op.f("pk_oauth_clients")),
    )
    op.create_table(
        "ai_notes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("security_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["securities.id"],
            name=op.f("fk_ai_notes_security_id_securities"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_ai_notes_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_notes")),
    )
    with op.batch_alter_table("ai_notes", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_ai_notes_security_id"), ["security_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_ai_notes_user_id_created_at"), ["user_id", "created_at"], unique=False
        )

    op.create_table(
        "mcp_calls",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("client", sa.String(length=200), nullable=True),
        sa.Column("tool", sa.String(length=64), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("result_chars", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_mcp_calls_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mcp_calls")),
    )
    with op.batch_alter_table("mcp_calls", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_mcp_calls_user_id_created_at"), ["user_id", "created_at"], unique=False
        )

    op.create_table(
        "mcp_tokens",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=100), nullable=True),
        sa.Column("prefix", sa.String(length=16), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('access', 'refresh', 'key')", name=op.f("ck_mcp_tokens_kind")),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["oauth_clients.client_id"],
            name=op.f("fk_mcp_tokens_client_id_oauth_clients"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_mcp_tokens_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("token_hash", name=op.f("pk_mcp_tokens")),
    )
    with op.batch_alter_table("mcp_tokens", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_mcp_tokens_client_id"), ["client_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_mcp_tokens_user_id_kind"), ["user_id", "kind"], unique=False
        )

    op.create_table(
        "oauth_codes",
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("code_challenge", sa.String(length=128), nullable=False),
        sa.Column("resource", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["oauth_clients.client_id"],
            name=op.f("fk_oauth_codes_client_id_oauth_clients"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_oauth_codes_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("code_hash", name=op.f("pk_oauth_codes")),
    )


def downgrade() -> None:

    op.drop_table("oauth_codes")
    with op.batch_alter_table("mcp_tokens", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_mcp_tokens_user_id_kind"))
        batch_op.drop_index(batch_op.f("ix_mcp_tokens_client_id"))

    op.drop_table("mcp_tokens")
    with op.batch_alter_table("mcp_calls", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_mcp_calls_user_id_created_at"))

    op.drop_table("mcp_calls")
    with op.batch_alter_table("ai_notes", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ai_notes_user_id_created_at"))
        batch_op.drop_index(batch_op.f("ix_ai_notes_security_id"))

    op.drop_table("ai_notes")
    op.drop_table("oauth_clients")
