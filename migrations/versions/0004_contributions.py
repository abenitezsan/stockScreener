"""aportaciones (dinero invertido), DCA mensual y fotos diarias de la cartera

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-08 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("(CURRENT_TIMESTAMP)")
MONEY = sa.Numeric(precision=20, scale=6)


def upgrade() -> None:
    op.create_table(
        "contributions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("kind IN ('initial', 'dca', 'adjust')", name=op.f("ck_contributions_kind")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_contributions_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contributions")),
        sa.UniqueConstraint("user_id", "external_id", name=op.f("uq_contributions_user_id")),
    )
    with op.batch_alter_table("contributions") as batch_op:
        batch_op.create_index(
            batch_op.f("ix_contributions_user_id_day"), ["user_id", "day"], unique=False
        )
    op.create_table(
        "dca_plans",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("day", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("applied_through", sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_dca_plans_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_dca_plans")),
    )
    op.create_table(
        "portfolio_snapshots",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("invested", MONEY, nullable=False),
        sa.Column("value", MONEY, nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_portfolio_snapshots_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "day", name=op.f("pk_portfolio_snapshots")),
    )


def downgrade() -> None:
    op.drop_table("portfolio_snapshots")
    op.drop_table("dca_plans")
    op.drop_table("contributions")
