from datetime import date

import pandas as pd
import pytest

from app.providers import yahoo
from app.providers.yahoo import YahooProvider, normalize_currency


def test_normalize_currency_pence():
    assert normalize_currency("GBp", 250.0) == ("GBP", 2.5)
    assert normalize_currency("EUR", 10.0) == ("EUR", 10.0)
    assert normalize_currency("GBp", None) == ("GBP", None)


def test_get_quotes_bulk(monkeypatch):
    idx = pd.to_datetime(["2026-09-24", "2026-09-25"])
    cols = pd.MultiIndex.from_product([["SAN.MC", "AAPL"], ["Close"]])
    df = pd.DataFrame([[8.0, 250.0], [8.2, float("nan")]], index=idx, columns=cols)
    monkeypatch.setattr(yahoo.yf, "download", lambda *a, **k: df)

    quotes = {q.symbol: q for q in YahooProvider().get_quotes(["SAN.MC", "AAPL", "MISSING"])}

    assert quotes["SAN.MC"].price == pytest.approx(8.2)
    assert quotes["SAN.MC"].previous_close == pytest.approx(8.0)
    assert quotes["AAPL"].price == 250.0 and quotes["AAPL"].previous_close is None
    assert "MISSING" not in quotes


class FakeTicker:
    def __init__(self, info, dividends=None):
        self.info = info
        self.dividends = dividends if dividends is not None else pd.Series(dtype=float)


def test_get_profile_normalizes_yield_and_pence(monkeypatch):
    info = {
        "longName": "Unilever PLC",
        "currency": "GBp",
        "dividendYield": 3.1,
        "fiveYearAvgDividendYield": 3.5,
        "payoutRatio": 0.72,
        "dividendRate": 150.0,
        "trailingPE": "Infinity",
    }
    monkeypatch.setattr(yahoo.yf, "Ticker", lambda s: FakeTicker(info))

    p = YahooProvider().get_profile("ULVR.L")

    assert p.currency == "GBP" and p.price_currency == "GBp"
    assert p.metrics["dividend_yield"] == pytest.approx(0.031)
    assert p.metrics["five_year_avg_dividend_yield"] == pytest.approx(0.035)
    assert p.metrics["payout_ratio"] == 0.72
    assert p.metrics["dividend_rate"] == pytest.approx(1.5)
    assert p.metrics["pe_ttm"] is None  # "Infinity"
    assert p.metrics["roe"] is None


def test_get_dividends_since(monkeypatch):
    series = pd.Series(
        [0.10, 0.12],
        index=pd.to_datetime(["2025-05-01", "2026-05-01"]).tz_localize("Europe/Madrid"),
    )
    monkeypatch.setattr(yahoo.yf, "Ticker", lambda s: FakeTicker({"currency": "EUR"}, series))

    divs = YahooProvider().get_dividends("SAN.MC", since=date(2026, 1, 1))

    assert [(d.ex_date, d.amount, d.currency) for d in divs] == [(date(2026, 5, 1), 0.12, "EUR")]
