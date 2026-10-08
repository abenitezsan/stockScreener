"""ajustes manuales del coste de una posición (editar el precio medio)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-09 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("(CURRENT_TIMESTAMP)")
MONEY = sa.Numeric(precision=20, scale=6)


def upgrade() -> None:
    op.create_table(
        "cost_adjustments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("security_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("avg_eur", MONEY, nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_cost_adjustments_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["securities.id"],
            name=op.f("fk_cost_adjustments_security_id_securities"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cost_adjustments")),
    )
    with op.batch_alter_table("cost_adjustments") as batch_op:
        batch_op.create_index(
            batch_op.f("ix_cost_adjustments_user_id_security_id"),
            ["user_id", "security_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("cost_adjustments")
