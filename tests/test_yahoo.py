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
    def __init__(self, info, income=None, cashflow=None):
        self.info = info
        self.income_stmt = income if income is not None else pd.DataFrame()
        self.cashflow = cashflow if cashflow is not None else pd.DataFrame()


def test_get_profile_normalizes_yield_pence_and_eps(monkeypatch):
    info = {
        "longName": "Unilever PLC",
        "currency": "GBp",
        "financialCurrency": "EUR",
        "currentPrice": 4500.0,
        "dividendYield": 3.1,
        "fiveYearAvgDividendYield": 3.5,
        "payoutRatio": 0.72,
        "dividendRate": 150.0,
        "trailingPE": 20.0,
        "forwardPE": "Infinity",
        "forwardEps": 2.5,
    }
    monkeypatch.setattr(yahoo.yf, "Ticker", lambda s: FakeTicker(info))

    p = YahooProvider().get_profile("ULVR.L")

    assert p.currency == "GBP" and p.price_currency == "GBp" and p.financial_currency == "EUR"
    assert p.metrics["dividend_yield"] == pytest.approx(0.031)
    assert p.metrics["five_year_avg_dividend_yield"] == pytest.approx(0.035)
    assert p.metrics["payout_ratio"] == 0.72
    assert p.metrics["dividend_rate"] == pytest.approx(1.5)
    assert p.metrics["eps_ttm"] == pytest.approx(45.0 / 20)  # en GBP, deducido del PER
    assert p.metrics["pe_forward"] is None  # "Infinity"
    assert p.metrics["eps_forward"] == 2.5  # sin PER: BPA publicado
    assert p.metrics["roe"] is None


def test_get_history_weekly_and_dividends(monkeypatch):
    idx = pd.bdate_range("2026-09-07", "2026-09-18")
    cols = pd.MultiIndex.from_product([["SAN.MC"], ["Close", "Dividends"]])
    closes = [float(i) for i in range(1, 11)]
    divs = [0.0] * 10
    divs[3] = 0.1
    df = pd.DataFrame(list(zip(closes, divs, strict=True)), index=idx, columns=cols)
    monkeypatch.setattr(yahoo.yf, "download", lambda *a, **k: df)

    [h] = YahooProvider().get_history(["SAN.MC"], "10y")

    assert h.weekly_closes == [(date(2026, 9, 11), 5.0), (date(2026, 9, 18), 10.0)]
    assert h.dividends == [(date(2026, 9, 10), 0.1)]


def test_get_financials(monkeypatch):
    periods = [pd.Timestamp("2024-12-31"), pd.Timestamp("2025-12-31")]
    income = pd.DataFrame(
        {periods[0]: [2.0, 100.0], periods[1]: [2.2, float("nan")]},
        index=["Diluted EPS", "Net Income"],
    )
    cashflow = pd.DataFrame(
        {periods[0]: [-50.0, 80.0], periods[1]: [-55.0, 90.0]},
        index=["Cash Dividends Paid", "Free Cash Flow"],
    )
    monkeypatch.setattr(yahoo.yf, "Ticker", lambda s: FakeTicker({}, income, cashflow))

    fins = YahooProvider().get_financials("X")

    assert [f.period_end for f in fins] == [date(2024, 12, 31), date(2025, 12, 31)]
    assert fins[1].eps == 2.2 and fins[1].net_income is None
    assert fins[1].dividends_paid == 55.0 and fins[1].free_cashflow == 90.0


def test_find_symbol_prefers_equity_on_requested_exchange(monkeypatch):
    class FakeSearch:
        def __init__(self, query, **kwargs):
            self.quotes = [
                {"symbol": "ANDRF", "quoteType": "EQUITY"},  # OTC de EE. UU.
                {"symbol": "ANDR.DE", "quoteType": "EQUITY"},
                {"symbol": "ANDR.VI", "quoteType": "EQUITY"},
            ]

    monkeypatch.setattr(yahoo.yf, "Search", FakeSearch)
    assert YahooProvider().find_symbol("Andritz AG", ".VI") == "ANDR.VI"
    assert YahooProvider().find_symbol("Andritz AG", ".MC") is None
