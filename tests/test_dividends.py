from datetime import date, timedelta

import pytest

from app.analysis.dividends import (
    analyze,
    annual_totals,
    cagr,
    split_specials,
    streaks,
    yield_band,
)

TODAY = date(2026, 9, 26)


def semiannual(first_year: int, last_year: int, start: float, growth: float):
    """Dividendo a cuenta (40 %) en enero y complementario (60 %) en junio."""
    payments, annual = [], start
    for year in range(first_year, last_year + 1):
        payments += [(date(year, 1, 15), annual * 0.4), (date(year, 6, 15), annual * 0.6)]
        annual *= 1 + growth
    return payments


def test_special_dividend_is_excluded_but_uneven_interim_final_is_not():
    payments = semiannual(2020, 2023, 1.0, 0.05) + [(date(2022, 9, 1), 3.0)]
    regular, specials = split_specials(payments)
    assert specials == [(date(2022, 9, 1), 3.0)]
    assert len(regular) == 8


def test_annual_totals_skip_current_and_partial_first_year():
    payments = [(date(2019, 6, 15), 0.6)] + semiannual(2020, 2026, 1.0, 0.0)
    annual = annual_totals(payments, TODAY)
    assert list(annual) == [2020, 2021, 2022, 2023, 2024, 2025]
    assert annual[2020] == pytest.approx(1.0)


def test_suspended_year_counts_as_zero():
    annual = annual_totals(
        [(date(2018, 6, 1), 1.0), (date(2019, 6, 1), 1.0), (date(2021, 6, 1), 0.5)], TODAY
    )
    assert annual[2020] == 0.0
    assert streaks(annual) == (0, 0)


def test_cagr_and_streaks():
    annual = {2019: 1.0, 2020: 0.8, 2021: 0.8, 2022: 0.85, 2023: 0.9, 2024: 1.0, 2025: 1.1}
    assert cagr(annual, 5) == pytest.approx((1.1 / 0.8) ** (1 / 5) - 1)
    assert cagr(annual, 10) is None
    # 2020 recorta: sin recorte desde entonces 5 años; subiendo 4 años (2021 igual)
    assert streaks(annual) == (5, 4)


def test_analyze_ttm():
    stats = analyze(semiannual(2015, 2026, 1.0, 0.05), TODAY)
    assert stats.dividend_ttm == pytest.approx(1.05**11)  # enero + junio de 2026
    assert stats.dgr_5y == pytest.approx(0.05)
    assert stats.dgr_10y == pytest.approx(0.05)
    assert stats.years_no_cut == 10


def weekly_closes(start: date, end: date, price_fn):
    closes, day = [], start
    while day <= end:
        closes.append((day, price_fn(day)))
        day += timedelta(days=7)
    return closes


def test_yield_band_constant_dividend_and_price():
    payments = [(date(y, 6, 1), 2.0) for y in range(2010, 2027)]
    closes = weekly_closes(date(2015, 1, 2), TODAY, lambda d: 50.0)
    avg, p80 = yield_band(payments, closes, TODAY, 5)
    assert avg == pytest.approx(0.04)
    assert p80 == pytest.approx(0.04)


def test_yield_band_needs_three_years():
    payments = [(date(y, 6, 1), 2.0) for y in range(2024, 2027)]
    closes = weekly_closes(date(2015, 1, 2), TODAY, lambda d: 50.0)
    assert yield_band(payments, closes, TODAY, 5) == (None, None)


def test_ttm_survives_late_annual_payment():
    # Pago anual que se retrasa 10 días respecto al año anterior
    payments = [(date(2024, 6, 1), 2.0), (date(2025, 6, 11), 2.0)]
    assert analyze(payments, date(2025, 6, 5)).dividend_ttm == 2.0
