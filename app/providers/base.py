"""Interfaz de proveedor de datos de mercado.

El resto de la aplicación solo depende de esta interfaz, de modo que Yahoo se puede
sustituir por otro proveedor (EODHD, FMP…) sin tocar el screener ni la cartera.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol


@dataclass(slots=True)
class QuoteData:
    symbol: str
    price: float | None
    previous_close: float | None
    currency: str | None
    as_of: datetime | None


@dataclass(slots=True)
class DividendData:
    symbol: str
    ex_date: date
    amount: float
    currency: str | None


@dataclass(slots=True)
class ProfileData:
    """Datos descriptivos y fundamentales de un valor. `metrics` usa los nombres de Fundamentals."""

    symbol: str
    name: str | None = None
    isin: str | None = None
    exchange: str | None = None
    country: str | None = None
    currency: str | None = None  # ISO (GBP)
    price_currency: str | None = None  # tal como cotiza en Yahoo (GBp = peniques)
    sector: str | None = None
    industry: str | None = None
    market_cap: float | None = None  # en `currency`
    metrics: dict[str, float | None] | None = None


class MarketDataProvider(Protocol):
    def get_quotes(self, symbols: list[str]) -> list[QuoteData]: ...

    def get_profile(self, symbol: str) -> ProfileData: ...

    def get_dividends(self, symbol: str, since: date | None = None) -> list[DividendData]: ...

    def get_fx_rates(self, currencies: list[str], base: str) -> dict[str, float]: ...
