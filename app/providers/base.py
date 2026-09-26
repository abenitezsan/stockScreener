"""Interfaz de proveedor de datos de mercado.

El resto de la aplicación solo depende de esta interfaz, de modo que Yahoo se puede
sustituir por otro proveedor (EODHD, FMP…) sin tocar el screener ni la cartera.

Unidades: `QuoteData` e `HistoryData` se devuelven tal como cotiza el valor (p. ej. peniques
para GBp); quien los guarda los normaliza con `Security.price_currency`. `ProfileData` ya viene
normalizado a la divisa ISO.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol


@dataclass(slots=True)
class QuoteData:
    symbol: str
    price: float | None
    previous_close: float | None
    as_of: datetime | None


@dataclass(slots=True)
class HistoryData:
    symbol: str
    weekly_closes: list[tuple[date, float]] = field(default_factory=list)
    dividends: list[tuple[date, float]] = field(default_factory=list)


@dataclass(slots=True)
class ProfileData:
    """Datos descriptivos y fundamentales de un valor. `metrics` usa los nombres de Fundamentals."""

    symbol: str
    name: str | None = None
    exchange: str | None = None
    country: str | None = None
    currency: str | None = None  # ISO (GBP)
    price_currency: str | None = None  # tal como cotiza en Yahoo (GBp = peniques)
    financial_currency: str | None = None
    sector: str | None = None
    industry: str | None = None
    quote_type: str | None = None  # EQUITY, ETF…
    metrics: dict[str, float | None] = field(default_factory=dict)


@dataclass(slots=True)
class FinancialsData:
    period_end: date
    revenue: float | None = None
    net_income: float | None = None
    eps: float | None = None
    operating_cashflow: float | None = None
    capex: float | None = None
    free_cashflow: float | None = None
    dividends_paid: float | None = None
    shares: float | None = None


class MarketDataProvider(Protocol):
    def get_quotes(self, symbols: list[str]) -> list[QuoteData]: ...

    def get_history(self, symbols: list[str], period: str) -> list[HistoryData]: ...

    def get_profile(self, symbol: str) -> ProfileData: ...

    def get_financials(self, symbol: str) -> list[FinancialsData]: ...

    def get_fx_rates(self, currencies: list[str], base: str) -> dict[str, float]: ...
