"""Proveedor basado en yfinance (Yahoo Finance, API no oficial).

Sufijos de Yahoo para los mercados de HeyTrade: .MC Madrid, .DE Xetra, .PA París, .AS Ámsterdam,
.MI Milán, .L Londres, .SW Suiza, .OL Oslo, .LS Lisboa, .HE Helsinki, .ST Estocolmo,
.CO Copenhague, .BR Bruselas, .VI Viena, .IR Dublín, .TO Toronto. EE. UU. sin sufijo.
"""

import logging
import math
from datetime import UTC

import pandas as pd
import yfinance as yf

from app.providers.base import FinancialsData, HistoryData, ProfileData, QuoteData

log = logging.getLogger(__name__)

# Campo de Fundamentals -> clave de `Ticker.info`
INFO_METRICS = {
    "market_cap": "marketCap",
    "pe_ttm": "trailingPE",
    "pe_forward": "forwardPE",
    "price_to_book": "priceToBook",
    "ev_to_ebitda": "enterpriseToEbitda",
    "dividend_yield": "dividendYield",
    "dividend_rate": "dividendRate",
    "payout_ratio": "payoutRatio",
    "five_year_avg_dividend_yield": "fiveYearAvgDividendYield",
    "roe": "returnOnEquity",
    "profit_margin": "profitMargins",
    "revenue_growth": "revenueGrowth",
    "earnings_growth": "earningsGrowth",
    "debt_to_equity": "debtToEquity",
    "beta": "beta",
    "week52_high": "fiftyTwoWeekHigh",
    "week52_low": "fiftyTwoWeekLow",
    "ma200": "twoHundredDayAverage",
    "target_mean_price": "targetMeanPrice",
}
# Métricas expresadas en unidades de precio (se convierten de peniques a libras)
PRICE_METRICS = ("dividend_rate", "week52_high", "week52_low", "ma200", "target_mean_price")

# Yahoo cotiza Londres en peniques (GBp) y Tel Aviv en agorot (ILA); normalizamos a la divisa ISO.
MINOR_CURRENCIES = {
    "GBp": ("GBP", 100),
    "GBX": ("GBP", 100),
    "ILA": ("ILS", 100),
    "ZAc": ("ZAR", 100),
}

# Filas de `income_stmt` / `cashflow` -> campo de FinancialsData (se usa la primera que exista)
INCOME_ROWS = {
    "revenue": ("Total Revenue", "Operating Revenue"),
    "net_income": ("Net Income Common Stockholders", "Net Income"),
    "eps": ("Diluted EPS", "Basic EPS"),
    "shares": ("Diluted Average Shares", "Basic Average Shares"),
}
CASHFLOW_ROWS = {
    "operating_cashflow": ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities"),
    "capex": ("Capital Expenditure",),
    "free_cashflow": ("Free Cash Flow",),
    "dividends_paid": ("Cash Dividends Paid", "Common Stock Dividend Paid"),
}


def normalize_currency(
    currency: str | None, value: float | None
) -> tuple[str | None, float | None]:
    if currency in MINOR_CURRENCIES:
        iso, divisor = MINOR_CURRENCIES[currency]
        return iso, None if value is None else value / divisor
    return currency, value


def price_divisor(price_currency: str | None) -> float:
    return MINOR_CURRENCIES.get(price_currency, (None, 1))[1]


def _num(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None  # Yahoo devuelve a veces "Infinity" o NaN


def _columns(df: pd.DataFrame, symbol: str) -> pd.DataFrame | None:
    try:
        return df[symbol]
    except KeyError:
        log.warning("Sin datos para %s", symbol)
        return None


def _statement_values(df: pd.DataFrame | None, rows: dict[str, tuple[str, ...]], out: dict) -> None:
    if df is None or df.empty:
        return
    for column in df.columns:
        period = out.setdefault(column.date(), {})
        for field, names in rows.items():
            name = next((n for n in names if n in df.index), None)
            if name is not None:
                period[field] = _num(df.at[name, column])


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
            data = _columns(df, symbol)
            if data is None:
                continue
            closes = data["Close"].dropna()
            if closes.empty:
                continue
            as_of = closes.index[-1].to_pydatetime()
            if as_of.tzinfo is None:
                as_of = as_of.replace(tzinfo=UTC)
            quotes.append(
                QuoteData(
                    symbol,
                    price=_num(closes.iloc[-1]),
                    previous_close=_num(closes.iloc[-2]) if len(closes) > 1 else None,
                    as_of=as_of,
                )
            )
        return quotes

    def get_history(self, symbols: list[str], period: str = "10y") -> list[HistoryData]:
        """Cierres diarios agregados a semanales y dividendos (fecha ex-dividendo), en bloque.

        `Close` y los dividendos de Yahoo vienen ajustados por splits, pero no por dividendos,
        que es lo que necesita el cálculo de la yield histórica.
        """
        if not symbols:
            return []
        df = yf.download(
            symbols,
            period=period,
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            actions=True,
            progress=False,
            threads=True,
        )
        result = []
        for symbol in symbols:
            data = _columns(df, symbol)
            if data is None:
                continue
            closes = data["Close"].dropna()
            weekly = closes.resample("W-FRI").last().dropna()
            dividends = data["Dividends"].dropna() if "Dividends" in data else pd.Series()
            result.append(
                HistoryData(
                    symbol,
                    weekly_closes=[(ts.date(), float(v)) for ts, v in weekly.items()],
                    dividends=[(ts.date(), float(v)) for ts, v in dividends.items() if v > 0],
                )
            )
        return result

    def get_profile(self, symbol: str) -> ProfileData:
        info = yf.Ticker(symbol).info or {}
        raw_currency = info.get("currency")
        currency, _ = normalize_currency(raw_currency, None)
        metrics = {field: _num(info.get(key)) for field, key in INFO_METRICS.items()}
        # Desde yfinance 0.2.5x `dividendYield` viene en % (3.1) y no en fracción (0.031);
        # `fiveYearAvgDividendYield` siempre ha venido en %. Guardamos siempre fracción.
        for field in ("dividend_yield", "five_year_avg_dividend_yield"):
            if metrics[field] is not None:
                metrics[field] /= 100
        for field in PRICE_METRICS:
            metrics[field] = normalize_currency(raw_currency, metrics[field])[1]
        # El BPA se deduce del PER para que esté en la divisa de cotización (las cuentas pueden
        # estar en otra, p. ej. Shell cotiza en GBp y reporta en USD).
        price = normalize_currency(
            raw_currency, _num(info.get("currentPrice") or info.get("regularMarketPrice"))
        )[1]
        for eps_field, pe_field, raw_key in (
            ("eps_ttm", "pe_ttm", "trailingEps"),
            ("eps_forward", "pe_forward", "forwardEps"),
        ):
            pe = metrics[pe_field]
            if price and pe:
                metrics[eps_field] = price / pe
            else:
                # Sin PER (p. ej. pérdidas): nos vale el signo del BPA publicado
                metrics[eps_field] = _num(info.get(raw_key))
        return ProfileData(
            symbol=symbol,
            name=info.get("longName") or info.get("shortName"),
            exchange=info.get("exchange"),
            country=info.get("country"),
            currency=currency,
            price_currency=raw_currency,
            financial_currency=info.get("financialCurrency"),
            sector=info.get("sector"),
            industry=info.get("industry"),
            quote_type=info.get("quoteType"),
            metrics=metrics,
        )

    def get_financials(self, symbol: str) -> list[FinancialsData]:
        ticker = yf.Ticker(symbol)
        periods: dict = {}
        _statement_values(ticker.income_stmt, INCOME_ROWS, periods)
        _statement_values(ticker.cashflow, CASHFLOW_ROWS, periods)
        result = []
        for period_end, values in sorted(periods.items()):
            if values.get("dividends_paid") is not None:
                values["dividends_paid"] = abs(values["dividends_paid"])
            if values.get("capex") is not None:
                values["capex"] = abs(values["capex"])
            result.append(FinancialsData(period_end=period_end, **values))
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
