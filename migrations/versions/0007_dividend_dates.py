"""fechas ex y de pago de los dividendos anunciados (calendario de Yahoo)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-02 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dividend_dates",
        sa.Column("security_id", sa.Integer(), nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=False),
        sa.Column("pay_date", sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["securities.id"],
            name=op.f("fk_dividend_dates_security_id_securities"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("security_id", "ex_date", name=op.f("pk_dividend_dates")),
    )


def downgrade() -> None:
    op.drop_table("dividend_dates")
