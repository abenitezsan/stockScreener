"""Precios justos, precio de compra y sostenibilidad del dividendo (docs/valoracion.md)."""

from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import fmean, median

from app.analysis import sectors
from app.analysis.dividends import Payment, analyze, yield_band

MIN_PE_YEARS = 3
MAX_PE = 60
MIN_CHOWDER_SPREAD = 0.02
MIN_TARGET_SPREAD = 0.005

# Flags que impiden el semáforo verde
HARD_FLAGS = {"payout_alto", "payout_fcf_alto", "payout_ocf_alto", "fcf_negativo", "bpa_negativo"}

# Umbrales de sostenibilidad por grupo: (payout BPA, payout FCF, payout flujo operativo)
PAYOUT_LIMITS: dict[str, tuple[float | None, float | None, float | None]] = {
    sectors.GENERAL: (0.70, 0.70, None),
    sectors.UTILITIES: (0.80, None, 0.80),
    sectors.REIT: (None, None, 0.90),
    sectors.FINANCIALS: (0.60, None, None),
    sectors.CYCLICAL: (0.80, 0.80, None),
}


@dataclass(slots=True)
class Params:
    window_years: int = 5
    margin_of_safety: float = 0.10
    target_total_return: float = 0.05
    gordon_discount_rate: float = 0.08
    growth_cap: float = 0.05


@dataclass(slots=True)
class AnnualFinancials:
    period_end: date
    eps: float | None = None
    free_cashflow: float | None = None
    operating_cashflow: float | None = None
    dividends_paid: float | None = None


@dataclass(slots=True)
class Inputs:
    today: date
    price: float | None
    payments: list[Payment]
    closes: list[tuple[date, float]]  # semanales, ordenados
    financials: list[AnnualFinancials] = field(default_factory=list)
    sector: str | None = None
    group: str = sectors.GENERAL
    eps_ttm: float | None = None
    eps_forward: float | None = None
    payout_ratio: float | None = None
    provider_yield: float | None = None  # yield de Yahoo, para detectar errores de unidades
    fin_to_price_fx: float = 1.0  # divisa de las cuentas -> divisa de cotización


def unit_scale(dividend_ttm: float, price: float | None, provider_yield: float | None) -> float:
    """Corrige dividendos publicados en peniques/libras equivocados (factor ×100).

    Solo actúa si la yield calculada es inverosímil por sí misma y la de Yahoo confirma el
    factor, para no estropear datos correctos si Yahoo cambia el formato de su yield.
    """
    if not (dividend_ttm and price and provider_yield):
        return 1.0
    computed = dividend_ttm / price
    ratio = computed / provider_yield
    if computed > 0.25 and 50 < ratio < 200:
        return 0.01
    if computed < 0.002 and 1 / 200 < ratio < 1 / 50:
        return 100.0
    return 1.0


def average_pe(inputs: Inputs, years: int) -> tuple[float | None, int]:
    pes = []
    for fin in sorted(inputs.financials, key=lambda f: f.period_end)[-years:]:
        if not fin.eps or fin.eps <= 0:
            continue
        window = [
            c
            for d, c in inputs.closes
            if fin.period_end - timedelta(days=365) < d <= fin.period_end
        ]
        if len(window) < 26:
            continue
        pe = fmean(window) / (fin.eps * inputs.fin_to_price_fx)
        if 0 < pe <= MAX_PE:
            pes.append(pe)
    if len(pes) < MIN_PE_YEARS:
        return None, len(pes)
    return fmean(pes), len(pes)


def chowder_threshold(current_yield: float | None, group: str) -> float:
    if current_yield is not None and group == sectors.UTILITIES and current_yield >= 0.04:
        return 0.08
    if current_yield is not None and current_yield < 0.03:
        return 0.15
    return 0.12


def max_price_for_target(
    dividend_forward: float | None, growth: float | None, target: float
) -> float | None:
    """Método 4: precio por encima del cual no se espera alcanzar la rentabilidad objetivo."""
    if not dividend_forward or growth is None or target - growth < MIN_TARGET_SPREAD:
        return None
    return dividend_forward / (target - growth)


def buy_price(
    fair_value: float | None,
    margin_of_safety: float,
    dividend_forward: float | None,
    growth: float | None,
    target: float,
) -> float | None:
    if fair_value is None:
        return None
    price = fair_value * (1 - margin_of_safety)
    ceiling = max_price_for_target(dividend_forward, growth, target)
    return min(price, ceiling) if ceiling is not None else price


def signal(price: float | None, buy: float | None, fair: float | None, quality_ok: bool | None):
    if price is None or buy is None or fair is None:
        return "none"
    if price <= buy and quality_ok:
        return "green"
    if price <= fair:
        return "yellow"
    return "red"


def _sustainability(inputs: Inputs, payout_fcf, payout_ocf, flags: list[str]) -> None:
    eps_limit, fcf_limit, ocf_limit = PAYOUT_LIMITS[inputs.group]
    if inputs.eps_ttm is not None and inputs.eps_ttm <= 0 and inputs.group != sectors.REIT:
        flags.append("bpa_negativo")
    checks = [
        (eps_limit, inputs.payout_ratio, "payout_alto"),
        (fcf_limit, payout_fcf, "payout_fcf_alto"),
        (ocf_limit, payout_ocf, "payout_ocf_alto"),
    ]
    for limit, value, flag in checks:
        if limit is None:
            continue
        if value is None:
            if not (flag == "payout_fcf_alto" and "fcf_negativo" in flags):
                flags.append("sin_datos_payout")
        elif value > limit:
            flags.append(flag)


def compute(inputs: Inputs, params: Params) -> dict:
    """Devuelve los campos de `Valuation` para un valor."""
    stats = analyze(inputs.payments, inputs.today)
    flags: list[str] = []

    scale = unit_scale(stats.dividend_ttm, inputs.price, inputs.provider_yield)
    if scale != 1.0:
        flags.append("dividendos_reescalados")
        stats = analyze([(d, a * scale) for d, a in inputs.payments], inputs.today)

    d = stats.dividend_ttm
    growth = min(max(stats.dgr_5y or 0.0, 0.0), params.growth_cap)
    d_forward = d * (1 + growth) if d else None
    current_yield = d / inputs.price if d and inputs.price else None

    avg5, p80_5 = yield_band(stats.regular, inputs.closes, inputs.today, params.window_years)
    avg10, p80_10 = yield_band(stats.regular, inputs.closes, inputs.today, 10)
    pe_avg, pe_years = average_pe(inputs, params.window_years)

    # Payout sobre flujos del último ejercicio con datos
    payout_fcf = payout_ocf = None
    latest = next(
        (
            f
            for f in sorted(inputs.financials, key=lambda f: f.period_end, reverse=True)
            if f.dividends_paid is not None
        ),
        None,
    )
    if latest is not None:
        if latest.free_cashflow is not None:
            if latest.free_cashflow > 0:
                payout_fcf = latest.dividends_paid / latest.free_cashflow
            elif inputs.group not in (sectors.FINANCIALS, sectors.UTILITIES, sectors.REIT):
                flags.append("fcf_negativo")
        if latest.operating_cashflow and latest.operating_cashflow > 0:
            payout_ocf = latest.dividends_paid / latest.operating_cashflow

    if not d:
        flags.append("sin_dividendo")
    else:
        _sustainability(inputs, payout_fcf, payout_ocf, flags)
    if len(stats.annual) < 3:
        flags.append("pocos_datos")
    if stats.years_no_cut is not None and stats.years_no_cut < 5 and len(stats.annual) > 5:
        flags.append("recorte_reciente")
    if current_yield and avg5 and current_yield > 1.5 * avg5:
        flags.append("yield_anomala")

    # Métodos de precio justo
    fv_yield = d / avg5 if d and avg5 else None
    buy_yield = d / p80_5 if d and p80_5 else None

    fv_pe = None
    eps = inputs.eps_forward if inputs.eps_forward and inputs.eps_forward > 0 else inputs.eps_ttm
    if pe_avg and eps and eps > 0 and inputs.group != sectors.REIT:
        fv_pe = eps * pe_avg

    threshold = chowder_threshold(current_yield, inputs.group)
    fv_chowder = None
    if d and stats.dgr_5y is not None and threshold - stats.dgr_5y >= MIN_CHOWDER_SPREAD:
        fv_chowder = d / (threshold - stats.dgr_5y)

    fv_gordon = None
    if d_forward and sectors.is_stable(inputs.group, inputs.sector):
        fv_gordon = d_forward / (params.gordon_discount_rate - growth)

    estimates = [v for v in (fv_yield, fv_pe, fv_chowder, fv_gordon) if v]
    fair = median(estimates) if estimates else None
    quality_ok = bool(d) and not HARD_FLAGS.intersection(flags)

    return {
        "dividend_ttm": d or None,
        "dividend_forward": d_forward,
        "dividend_last_year": stats.dividend_last_year,
        "dgr_5y": stats.dgr_5y,
        "dgr_10y": stats.dgr_10y,
        "growth_used": growth,
        "years_no_cut": stats.years_no_cut,
        "years_growth": stats.years_growth,
        "yield_avg_5y": avg5,
        "yield_p80_5y": p80_5,
        "yield_avg_10y": avg10,
        "yield_p80_10y": p80_10,
        "pe_avg": pe_avg,
        "pe_years": pe_years,
        "payout_fcf": payout_fcf,
        "payout_ocf": payout_ocf,
        "fv_yield": fv_yield,
        "buy_yield": buy_yield,
        "fv_pe": fv_pe,
        "fv_chowder": fv_chowder,
        "chowder_threshold": threshold,
        "fv_gordon": fv_gordon,
        "max_price_target": max_price_for_target(d_forward, growth, params.target_total_return),
        "fair_value": fair,
        "buy_price": buy_price(
            fair, params.margin_of_safety, d_forward, growth, params.target_total_return
        ),
        "quality_ok": quality_ok,
        "flags": sorted(set(flags)),
    }
