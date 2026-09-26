from datetime import date, timedelta

import pytest

from app.analysis import sectors
from app.analysis.valuation import (
    AnnualFinancials,
    Inputs,
    Params,
    buy_price,
    chowder_threshold,
    compute,
    max_price_for_target,
    signal,
    unit_scale,
)

TODAY = date(2026, 9, 26)


def weekly(start: date, price: float):
    out, day = [], start
    while day <= TODAY:
        out.append((day, price))
        day += timedelta(days=7)
    return out


def quarterly(first_year: int, amount: float, growth: float):
    out = []
    for i, year in enumerate(range(first_year, TODAY.year + 1)):
        q = amount * (1 + growth) ** i / 4
        out += [(date(year, m, 10), q) for m in (2, 5, 8, 11) if date(year, m, 10) <= TODAY]
    return out


def financials(eps: float, fcf: float, ocf: float, paid: float):
    return [
        AnnualFinancials(
            date(y, 12, 31), eps=eps, free_cashflow=fcf, operating_cashflow=ocf, dividends_paid=paid
        )
        for y in range(2021, 2026)
    ]


def base_inputs(**kw):
    defaults = {
        "today": TODAY,
        "price": 50.0,
        "payments": quarterly(2014, 2.0, 0.0),
        "closes": weekly(date(2015, 1, 2), 50.0),
        "financials": financials(eps=4.0, fcf=500.0, ocf=800.0, paid=200.0),
        "eps_ttm": 4.0,
        "eps_forward": 4.0,
        "payout_ratio": 0.5,
        "provider_yield": 0.04,
    }
    defaults.update(kw)
    return Inputs(**defaults)


def test_flat_dividend_company():
    v = compute(base_inputs(), Params())
    assert v["dividend_ttm"] == pytest.approx(2.0)
    assert v["dgr_5y"] == pytest.approx(0.0)
    assert v["yield_avg_5y"] == pytest.approx(0.04)
    assert v["fv_yield"] == pytest.approx(50.0)
    assert v["pe_avg"] == pytest.approx(12.5) and v["pe_years"] == 5
    assert v["fv_pe"] == pytest.approx(50.0)
    # Chowder: yield 4 % -> umbral 12 %, g = 0 -> 2 / 0.12
    assert v["fv_chowder"] == pytest.approx(2.0 / 0.12)
    assert v["fv_gordon"] is None  # sector general, no estable
    assert v["fair_value"] == pytest.approx(50.0)  # mediana de 50, 50, 16.7
    # Método 4: g = 0, objetivo 5 % -> 2 / 0.05 = 40 < 50 × 0.9
    assert v["max_price_target"] == pytest.approx(40.0)
    assert v["buy_price"] == pytest.approx(40.0)
    assert v["payout_fcf"] == pytest.approx(0.4)
    assert v["quality_ok"] is True
    assert v["flags"] == []


def test_gordon_only_for_stable_sectors():
    v = compute(base_inputs(group=sectors.UTILITIES, sector="Utilities"), Params())
    assert v["fv_gordon"] == pytest.approx(2.0 / 0.08)


def test_growth_is_capped():
    v = compute(base_inputs(payments=quarterly(2014, 1.0, 0.10)), Params())
    assert v["dgr_5y"] == pytest.approx(0.10)
    assert v["growth_used"] == pytest.approx(0.05)
    assert v["dividend_forward"] == pytest.approx(v["dividend_ttm"] * 1.05)
    assert v["max_price_target"] is None  # g >= objetivo


def test_high_payout_blocks_quality():
    v = compute(base_inputs(payout_ratio=0.95), Params())
    assert "payout_alto" in v["flags"]
    assert v["quality_ok"] is False


def test_negative_fcf_flag_not_for_utilities():
    fin = financials(eps=4.0, fcf=-100.0, ocf=800.0, paid=200.0)
    assert "fcf_negativo" in compute(base_inputs(financials=fin), Params())["flags"]
    util = compute(base_inputs(financials=fin, group=sectors.UTILITIES), Params())
    assert "fcf_negativo" not in util["flags"]
    assert util["payout_ocf"] == pytest.approx(0.25)
    assert util["quality_ok"] is True


def test_no_dividend():
    v = compute(base_inputs(payments=[]), Params())
    assert v["dividend_ttm"] is None
    assert v["fair_value"] == pytest.approx(50.0)  # solo PER
    assert v["quality_ok"] is False
    assert "sin_dividendo" in v["flags"]


def test_pence_mixup_is_rescaled():
    pence = [(d, a * 100) for d, a in quarterly(2014, 2.0, 0.0)]
    v = compute(base_inputs(payments=pence), Params())
    assert v["dividend_ttm"] == pytest.approx(2.0)
    assert "dividendos_reescalados" in v["flags"]
    assert unit_scale(2.0, 50.0, 0.04) == 1.0
    # Yield de Yahoo 100× menor pero la calculada es plausible: no se toca
    assert unit_scale(2.0, 50.0, 0.0004) == 1.0
    assert unit_scale(0.02, 50.0, 0.04) == 100.0


def test_chowder_thresholds():
    assert chowder_threshold(0.05, sectors.UTILITIES) == 0.08
    assert chowder_threshold(0.05, sectors.GENERAL) == 0.12
    assert chowder_threshold(0.02, sectors.GENERAL) == 0.15


def test_buy_price_and_signal():
    assert max_price_for_target(2.0, 0.03, 0.05) == pytest.approx(100.0)
    assert buy_price(50.0, 0.10, 2.0, 0.03, 0.05) == pytest.approx(45.0)
    assert buy_price(None, 0.10, 2.0, 0.03, 0.05) is None
    assert signal(40, 45, 50, True) == "green"
    assert signal(40, 45, 50, False) == "yellow"
    assert signal(48, 45, 50, True) == "yellow"
    assert signal(55, 45, 50, True) == "red"
    assert signal(55, None, None, True) == "none"
