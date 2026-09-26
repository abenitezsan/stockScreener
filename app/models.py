"""Modelo de datos. Todas las tablas viven en el esquema configurado (DB_SCHEMA)."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import get_settings

Money = Numeric(20, 6)


class Base(DeclarativeBase):
    metadata = MetaData(
        schema=get_settings().db_schema,
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_N_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        },
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- Universo y datos de mercado -------------------------------------------------


class Security(Base):
    """Un valor cotizado. `symbol` es el ticker de Yahoo (p. ej. SAN.MC, ENB.TO, AAPL)."""

    __tablename__ = "securities"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str | None] = mapped_column(String(255))
    isin: Mapped[str | None] = mapped_column(String(12), index=True)
    exchange: Mapped[str | None] = mapped_column(String(32))
    country: Mapped[str | None] = mapped_column(String(64), index=True)
    currency: Mapped[str | None] = mapped_column(String(3))  # ISO: GBP
    price_currency: Mapped[str | None] = mapped_column(String(3))  # como cotiza: GBp (peniques)
    sector: Mapped[str | None] = mapped_column(String(128), index=True)
    industry: Mapped[str | None] = mapped_column(String(128))
    # Índices o listas de origen por los que el valor entró en el universo (SP500, STOXX600, manual…)
    universes: Mapped[list[str]] = mapped_column(ARRAY(String(32)), server_default="{}")
    active: Mapped[bool] = mapped_column(server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Quote(Base):
    """Última cotización conocida (una fila por valor, se sobrescribe en cada refresco)."""

    __tablename__ = "quotes"

    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), primary_key=True
    )
    price: Mapped[Decimal | None] = mapped_column(Money)
    previous_close: Mapped[Decimal | None] = mapped_column(Money)
    change_pct: Mapped[float | None] = mapped_column(Double)
    currency: Mapped[str | None] = mapped_column(String(3))
    as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PriceHistory(Base):
    """Cierres diarios, para gráficos y métricas propias (máx./mín. 52 semanas, volatilidad…)."""

    __tablename__ = "price_history"

    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    close: Mapped[Decimal] = mapped_column(Money)
    adj_close: Mapped[Decimal | None] = mapped_column(Money)


class Fundamentals(Base):
    """Métricas del screener. Columnas planas para filtrar y ordenar directamente en SQL."""

    __tablename__ = "fundamentals"

    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), primary_key=True
    )
    market_cap_eur: Mapped[float | None] = mapped_column(Double, index=True)
    pe_ttm: Mapped[float | None] = mapped_column(Double, index=True)
    pe_forward: Mapped[float | None] = mapped_column(Double)
    price_to_book: Mapped[float | None] = mapped_column(Double)
    price_to_sales: Mapped[float | None] = mapped_column(Double)
    ev_to_ebitda: Mapped[float | None] = mapped_column(Double)
    dividend_yield: Mapped[float | None] = mapped_column(Double, index=True)
    dividend_rate: Mapped[float | None] = mapped_column(Double)
    payout_ratio: Mapped[float | None] = mapped_column(Double)
    five_year_avg_dividend_yield: Mapped[float | None] = mapped_column(Double)
    dividend_growth_years: Mapped[int | None] = mapped_column()
    roe: Mapped[float | None] = mapped_column(Double)
    roa: Mapped[float | None] = mapped_column(Double)
    profit_margin: Mapped[float | None] = mapped_column(Double)
    operating_margin: Mapped[float | None] = mapped_column(Double)
    revenue_growth: Mapped[float | None] = mapped_column(Double)
    earnings_growth: Mapped[float | None] = mapped_column(Double)
    debt_to_equity: Mapped[float | None] = mapped_column(Double)
    current_ratio: Mapped[float | None] = mapped_column(Double)
    beta: Mapped[float | None] = mapped_column(Double)
    week52_high: Mapped[float | None] = mapped_column(Double)
    week52_low: Mapped[float | None] = mapped_column(Double)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DividendEvent(Base):
    """Dividendos anunciados/pagados por la empresa (por acción), según el proveedor de datos."""

    __tablename__ = "dividend_events"
    __table_args__ = (UniqueConstraint("security_id", "ex_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    security_id: Mapped[int] = mapped_column(ForeignKey("securities.id", ondelete="CASCADE"))
    ex_date: Mapped[date] = mapped_column(Date)
    pay_date: Mapped[date | None] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str | None] = mapped_column(String(3))


class FxRate(Base):
    """Tipo de cambio diario: 1 unidad de `currency` = `rate_to_base` unidades de la divisa base."""

    __tablename__ = "fx_rates"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    rate_to_base: Mapped[Decimal] = mapped_column(Numeric(20, 10))


# --- Cartera ---------------------------------------------------------------------


class Transaction(Base):
    """Operación de compra/venta. Las posiciones se calculan a partir de aquí."""

    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("kind IN ('buy', 'sell')", name="kind"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        Index(None, "security_id", "trade_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    security_id: Mapped[int] = mapped_column(ForeignKey("securities.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(String(8))
    trade_date: Mapped[date] = mapped_column(Date)
    quantity: Mapped[Decimal] = mapped_column(Money)
    price: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    # Tipo de cambio aplicado por el bróker (divisa de la operación -> EUR)
    fx_rate: Mapped[Decimal] = mapped_column(Numeric(20, 10), server_default="1")
    fees: Mapped[Decimal] = mapped_column(Money, server_default="0")  # en divisa base
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DividendPayment(Base):
    """Dividendo cobrado (o previsto) por la cartera."""

    __tablename__ = "dividend_payments"
    __table_args__ = (
        CheckConstraint("status IN ('expected', 'received')", name="status"),
        Index(None, "pay_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    security_id: Mapped[int] = mapped_column(ForeignKey("securities.id", ondelete="RESTRICT"))
    dividend_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("dividend_events.id", ondelete="SET NULL"), unique=True
    )
    status: Mapped[str] = mapped_column(String(10), server_default="expected")
    ex_date: Mapped[date | None] = mapped_column(Date)
    pay_date: Mapped[date] = mapped_column(Date)
    shares: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    # Importes bruto y retenciones en la divisa del dividendo
    gross: Mapped[Decimal] = mapped_column(Money)
    withholding_origin: Mapped[Decimal] = mapped_column(Money, server_default="0")
    withholding_domestic: Mapped[Decimal] = mapped_column(Money, server_default="0")
    fx_rate: Mapped[Decimal] = mapped_column(Numeric(20, 10), server_default="1")
    net_base: Mapped[Decimal | None] = mapped_column(Money)  # neto cobrado en EUR
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
