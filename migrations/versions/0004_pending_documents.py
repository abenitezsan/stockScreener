"""documentos del buzón pendientes de asignar ticker

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-08 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pending_documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("isin", sa.String(length=12), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_pending_documents_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pending_documents")),
        sa.UniqueConstraint("user_id", "sha256", name=op.f("uq_pending_documents_user_id")),
    )
    with op.batch_alter_table("pending_documents") as batch_op:
        batch_op.create_index(batch_op.f("ix_pending_documents_isin"), ["isin"], unique=False)


def downgrade() -> None:
    op.drop_table("pending_documents")
