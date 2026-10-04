"""cartera por usuario: operaciones y dividendos cobrados

Las tablas no se usaban todavía (estaban vacías), así que se recrean.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-04 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("(CURRENT_TIMESTAMP)")
MONEY = sa.Numeric(precision=20, scale=6)


def upgrade() -> None:
    op.drop_table("dividend_payments")
    op.drop_table("transactions")
    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("security_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("quantity", MONEY, nullable=False),
        sa.Column("price", MONEY, nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "fx_rate", sa.Numeric(precision=20, scale=10), server_default="1", nullable=False
        ),
        sa.Column("fees", MONEY, server_default="0", nullable=False),
        sa.Column("total_eur", MONEY, nullable=False),
        sa.Column("source", sa.String(length=10), server_default="manual", nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("kind IN ('buy', 'sell')", name=op.f("ck_transactions_kind")),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_transactions_quantity_positive")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_transactions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["securities.id"],
            name=op.f("fk_transactions_security_id_securities"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transactions")),
        sa.UniqueConstraint("user_id", "external_id", name=op.f("uq_transactions_user_id")),
    )
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.create_index(
            batch_op.f("ix_transactions_user_id_security_id_trade_date"),
            ["user_id", "security_id", "trade_date"],
            unique=False,
        )
    op.create_table(
        "dividend_payments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("security_id", sa.Integer(), nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=True),
        sa.Column("pay_date", sa.Date(), nullable=False),
        sa.Column("shares", MONEY, nullable=False),
        sa.Column("per_share", MONEY, nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("gross", MONEY, nullable=False),
        sa.Column("withholding_origin", MONEY, server_default="0", nullable=False),
        sa.Column("withholding_domestic", MONEY, server_default="0", nullable=False),
        sa.Column("withholding_rate", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("fees", MONEY, server_default="0", nullable=False),
        sa.Column(
            "fx_rate", sa.Numeric(precision=20, scale=10), server_default="1", nullable=False
        ),
        sa.Column("net_base", MONEY, nullable=False),
        sa.Column("source", sa.String(length=10), server_default="manual", nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_dividend_payments_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["securities.id"],
            name=op.f("fk_dividend_payments_security_id_securities"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dividend_payments")),
        sa.UniqueConstraint("user_id", "external_id", name=op.f("uq_dividend_payments_user_id")),
    )
    with op.batch_alter_table("dividend_payments") as batch_op:
        batch_op.create_index(
            batch_op.f("ix_dividend_payments_user_id_pay_date"),
            ["user_id", "pay_date"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("dividend_payments")
    op.drop_table("transactions")
    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("security_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("quantity", MONEY, nullable=False),
        sa.Column("price", MONEY, nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "fx_rate", sa.Numeric(precision=20, scale=10), server_default="1", nullable=False
        ),
        sa.Column("fees", MONEY, server_default="0", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("kind IN ('buy', 'sell')", name=op.f("ck_transactions_kind")),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_transactions_quantity_positive")),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["securities.id"],
            name=op.f("fk_transactions_security_id_securities"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transactions")),
    )
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.create_index(
            batch_op.f("ix_transactions_security_id_trade_date"),
            ["security_id", "trade_date"],
            unique=False,
        )
    op.create_table(
        "dividend_payments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("security_id", sa.Integer(), nullable=False),
        sa.Column("dividend_event_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=10), server_default="expected", nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=True),
        sa.Column("pay_date", sa.Date(), nullable=False),
        sa.Column("shares", MONEY, nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("gross", MONEY, nullable=False),
        sa.Column("withholding_origin", MONEY, server_default="0", nullable=False),
        sa.Column("withholding_domestic", MONEY, server_default="0", nullable=False),
        sa.Column(
            "fx_rate", sa.Numeric(precision=20, scale=10), server_default="1", nullable=False
        ),
        sa.Column("net_base", MONEY, nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "status IN ('expected', 'received')", name=op.f("ck_dividend_payments_status")
        ),
        sa.ForeignKeyConstraint(
            ["dividend_event_id"],
            ["dividend_events.id"],
            name=op.f("fk_dividend_payments_dividend_event_id_dividend_events"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["securities.id"],
            name=op.f("fk_dividend_payments_security_id_securities"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dividend_payments")),
        sa.UniqueConstraint(
            "dividend_event_id", name=op.f("uq_dividend_payments_dividend_event_id")
        ),
    )
    with op.batch_alter_table("dividend_payments") as batch_op:
        batch_op.create_index(
            batch_op.f("ix_dividend_payments_pay_date"), ["pay_date"], unique=False
        )
