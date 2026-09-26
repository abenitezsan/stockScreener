"""Proveedor de datos sintéticos para pruebas y para ver la interfaz sin conexión a Yahoo."""

from datetime import UTC, date, datetime, timedelta

from app.config import today
from app.providers.base import FinancialsData, HistoryData, ProfileData, QuoteData

TODAY = today()

# símbolo: (divisa Yahoo, sector, industria, precio, dividendo anual inicial, crecimiento, pagos/año)
SPECS = {
    "AAA": ("USD", "Consumer Defensive", "Household Products", 60.0, 1.6, 0.06, 4),
    "BBB.MC": ("EUR", "Utilities", "Utilities - Regulated Electric", 20.0, 0.8, 0.03, 2),
    "CCC.L": ("GBp", "Energy", "Oil & Gas Integrated", 2500.0, 80.0, 0.04, 4),
    "DDD": ("USD", "Technology", "Software - Infrastructure", 300.0, 0.0, 0.0, 0),
}


def _price(symbol: str, day: date) -> float:
    _, _, _, price, *_ = SPECS[symbol]
    years_ago = (TODAY - day).days / 365.25
    return price * (1 - 0.03 * years_ago) * (1 + 0.05 * ((day.toordinal() % 91) / 91 - 0.5))


def _dividends(symbol: str) -> list[tuple[date, float]]:
    _, _, _, _, start, growth, per_year = SPECS[symbol]
    if not per_year:
        return []
    out = []
    for i, year in enumerate(range(TODAY.year - 11, TODAY.year + 1)):
        for k in range(per_year):
            day = date(year, 1 + k * (12 // per_year), 15)
            if day <= TODAY:
                out.append((day, start * (1 + growth) ** i / per_year))
    return out


class FakeProvider:
    def get_quotes(self, symbols):
        now = datetime.now(UTC)
        return [
            QuoteData(s, _price(s, TODAY), _price(s, TODAY - timedelta(days=1)), now)
            for s in symbols
            if s in SPECS
        ] + [
            QuoteData(s, 1.0 if s.startswith("USD") else 0.85, None, now)
            for s in symbols
            if s.endswith("=X")
        ]

    def get_history(self, symbols, period):
        years = 10 if period == "10y" else 0.25
        start = TODAY - timedelta(days=round(365.25 * years))
        result = []
        for s in symbols:
            weeks, day = [], start + timedelta(days=(4 - start.weekday()) % 7)
            while day <= TODAY:
                weeks.append((day, _price(s, day)))
                day += timedelta(days=7)
            result.append(HistoryData(s, weeks, [d for d in _dividends(s) if d[0] >= start]))
        return result

    def get_profile(self, symbol):
        if symbol not in SPECS:
            return ProfileData(symbol)
        ccy, sector, industry, *_ = SPECS[symbol]
        iso = "GBP" if ccy == "GBp" else ccy
        unit = 100 if ccy == "GBp" else 1
        p = _price(symbol, TODAY) / unit
        return ProfileData(
            symbol=symbol,
            name=f"{symbol} Corp",
            country="Test",
            currency=iso,
            price_currency=ccy,
            financial_currency="USD" if symbol == "CCC.L" else iso,
            sector=sector,
            industry=industry,
            metrics={
                "market_cap": 5e10,
                "pe_ttm": 15.0,
                "pe_forward": 14.0,
                "eps_ttm": p / 15,
                "eps_forward": p / 14,
                "payout_ratio": 0.55,
                "dividend_yield": None,
                "week52_high": p * 1.1,
                "week52_low": p * 0.85,
                "ma200": p * 0.98,
            },
        )

    def get_financials(self, symbol):
        ccy, *_ = SPECS[symbol]
        unit = 100 if ccy == "GBp" else 1
        return [
            FinancialsData(
                period_end=date(y, 12, 31),
                eps=_price(symbol, date(y, 12, 31)) / unit / 15,
                free_cashflow=1000.0,
                operating_cashflow=1500.0,
                dividends_paid=500.0,
            )
            for y in range(TODAY.year - 4, TODAY.year)
        ]

    def get_fx_rates(self, currencies, base):
        rates = {
            "USD": 0.9,
            "GBP": 1.17,
            "CAD": 0.66,
            "CHF": 1.06,
            "SEK": 0.087,
            "NOK": 0.085,
            "DKK": 0.134,
        }
        return {base: 1.0, **{c: rates.get(c, 1.0) for c in currencies if c != base}}
