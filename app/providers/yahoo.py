"""Proveedor basado en yfinance (Yahoo Finance, API no oficial).

Sufijos de Yahoo para los mercados de HeyTrade: .MC Madrid, .DE Xetra, .PA París, .AS Ámsterdam,
.MI Milán, .L Londres, .SW Suiza, .OL Oslo, .LS Lisboa, .HE Helsinki, .ST Estocolmo,
.CO Copenhague, .BR Bruselas, .VI Viena, .IR Dublín, .TO Toronto. EE. UU. sin sufijo.
"""

import logging
import math
from datetime import UTC, date

import yfinance as yf

from app.providers.base import DividendData, ProfileData, QuoteData

log = logging.getLogger(__name__)

# Campo de Fundamentals -> clave de `Ticker.info`
INFO_METRICS = {
    "pe_ttm": "trailingPE",
    "pe_forward": "forwardPE",
    "price_to_book": "priceToBook",
    "price_to_sales": "priceToSalesTrailing12Months",
    "ev_to_ebitda": "enterpriseToEbitda",
    "dividend_yield": "dividendYield",
    "dividend_rate": "dividendRate",
    "payout_ratio": "payoutRatio",
    "five_year_avg_dividend_yield": "fiveYearAvgDividendYield",
    "roe": "returnOnEquity",
    "roa": "returnOnAssets",
    "profit_margin": "profitMargins",
    "operating_margin": "operatingMargins",
    "revenue_growth": "revenueGrowth",
    "earnings_growth": "earningsGrowth",
    "debt_to_equity": "debtToEquity",
    "current_ratio": "currentRatio",
    "beta": "beta",
    "week52_high": "fiftyTwoWeekHigh",
    "week52_low": "fiftyTwoWeekLow",
}

# Yahoo cotiza Londres en peniques (GBp) y Tel Aviv en agorot (ILA); normalizamos a la divisa ISO.
MINOR_CURRENCIES = {
    "GBp": ("GBP", 100),
    "GBX": ("GBP", 100),
    "ILA": ("ILS", 100),
    "ZAc": ("ZAR", 100),
}


def normalize_currency(
    currency: str | None, value: float | None
) -> tuple[str | None, float | None]:
    if currency in MINOR_CURRENCIES:
        iso, divisor = MINOR_CURRENCIES[currency]
        return iso, None if value is None else value / divisor
    return currency, value


def _num(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None  # Yahoo devuelve a veces "Infinity" o NaN


class YahooProvider:
    def get_quotes(self, symbols: list[str]) -> list[QuoteData]:
        """Cotizaciones en bloque: una sola petición para muchos símbolos."""
        if not symbols:
            return []
        df = yf.download(
            symbols,
            period="5d",
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True,
        )
        quotes = []
        for symbol in symbols:
            try:
                closes = df[symbol]["Close"].dropna()
            except KeyError:
                log.warning("Sin cotización para %s", symbol)
                continue
            if closes.empty:
                continue
            price = _num(closes.iloc[-1])
            prev = _num(closes.iloc[-2]) if len(closes) > 1 else None
            as_of = closes.index[-1].to_pydatetime()
            if as_of.tzinfo is None:
                as_of = as_of.replace(tzinfo=UTC)
            # La divisa no viene en `download`: el precio está en Security.price_currency
            # (p. ej. GBp) y se normaliza con normalize_currency() al guardarlo.
            quotes.append(QuoteData(symbol, price, prev, None, as_of))
        return quotes

    def get_profile(self, symbol: str) -> ProfileData:
        info = yf.Ticker(symbol).info or {}
        currency, _ = normalize_currency(info.get("currency"), None)
        metrics = {field: _num(info.get(key)) for field, key in INFO_METRICS.items()}
        # Desde yfinance 0.2.5x `dividendYield` viene en % (3.1) y no en fracción (0.031);
        # guardamos siempre fracción, igual que payoutRatio, roe, márgenes…
        for field in ("dividend_yield", "five_year_avg_dividend_yield"):
            if metrics[field] is not None:
                metrics[field] /= 100
        if info.get("currency") in MINOR_CURRENCIES:
            for field in ("dividend_rate", "week52_high", "week52_low"):
                metrics[field] = normalize_currency(info["currency"], metrics[field])[1]
        return ProfileData(
            symbol=symbol,
            name=info.get("longName") or info.get("shortName"),
            isin=None,  # `Ticker.isin` hace una petición extra y poco fiable; se rellena aparte
            exchange=info.get("exchange"),
            country=info.get("country"),
            currency=currency,
            price_currency=info.get("currency"),
            sector=info.get("sector"),
            industry=info.get("industry"),
            market_cap=_num(info.get("marketCap")),
            metrics=metrics,
        )

    def get_dividends(self, symbol: str, since: date | None = None) -> list[DividendData]:
        ticker = yf.Ticker(symbol)
        series = ticker.dividends
        if series is None or series.empty:
            return []
        raw_currency = (ticker.info or {}).get("currency")
        result = []
        for ts, amount in series.items():
            ex_date = ts.date()
            if since and ex_date < since:
                continue
            currency, value = normalize_currency(raw_currency, _num(amount))
            if value is not None:
                result.append(DividendData(symbol, ex_date, value, currency))
        return result

    def get_fx_rates(self, currencies: list[str], base: str) -> dict[str, float]:
        """Devuelve {divisa: unidades de `base` por 1 unidad de divisa}. Usa pares tipo USDEUR=X."""
        others = sorted({c for c in currencies if c and c != base})
        rates = {base: 1.0}
        if not others:
            return rates
        pairs = [f"{c}{base}=X" for c in others]
        for quote in self.get_quotes(pairs):
            if quote.price:
                rates[quote.symbol[:3]] = quote.price
        return rates
