"""Análisis del histórico de dividendos. Funciones puras, sin acceso a base de datos."""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import accumulate
from statistics import fmean, quantiles

SPECIAL_FACTOR = 2.0  # un pago > 2× el mayor de los 400 días previos se considera extraordinario
SPECIAL_LOOKBACK = timedelta(days=400)
CUT_TOLERANCE = 0.01  # bajadas de menos del 1 % no cuentan como recorte
MIN_BAND_WEEKS = 150  # ~3 años de datos semanales para la banda de yield
# Un pago que llega unos días más tarde que el año anterior dejaría el TTM a 0 durante unas
# semanas; se toma el máximo con el TTM de hace 45 días (a cambio, un recorte se refleja con
# ese retraso).
TTM_GRACE = timedelta(days=45)

Payment = tuple[date, float]


@dataclass(slots=True)
class DividendStats:
    regular: list[Payment]
    specials: list[Payment]
    annual: dict[int, float]
    dividend_ttm: float
    dividend_last_year: float | None
    dgr_5y: float | None
    dgr_10y: float | None
    years_no_cut: int | None
    years_growth: int | None


def split_specials(payments: list[Payment]) -> tuple[list[Payment], list[Payment]]:
    payments = sorted(p for p in payments if p[1] > 0)
    regular, specials = [], []
    for i, (day, amount) in enumerate(payments):
        previous = [a for d, a in payments[:i] if d >= day - SPECIAL_LOOKBACK]
        if previous and amount > SPECIAL_FACTOR * max(previous):
            specials.append((day, amount))
        else:
            regular.append((day, amount))
    return regular, specials


def annual_totals(regular: list[Payment], today: date) -> dict[int, float]:
    """Dividendo por año natural, sin el año en curso ni un primer año incompleto."""
    totals: dict[int, float] = defaultdict(float)
    counts: dict[int, int] = defaultdict(int)
    for day, amount in regular:
        if day.year < today.year:
            totals[day.year] += amount
            counts[day.year] += 1
    years = sorted(totals)
    if len(years) >= 2 and counts[years[0]] < counts[years[1]]:
        del totals[years[0]]
    # Años sin pago entre el primero y el último: dividendo suspendido
    if totals:
        for year in range(min(totals), max(totals) + 1):
            totals.setdefault(year, 0.0)
    return dict(sorted(totals.items()))


def cagr(annual: dict[int, float], years: int) -> float | None:
    if not annual:
        return None
    last = max(annual)
    start, end = annual.get(last - years), annual[last]
    if not start or start <= 0 or end <= 0:
        return None
    return (end / start) ** (1 / years) - 1


def streaks(annual: dict[int, float]) -> tuple[int | None, int | None]:
    """(años consecutivos sin recorte, años consecutivos subiendo), contando desde el último."""
    years = sorted(annual)
    if len(years) < 2:
        return None, None
    no_cut = growth = 0
    growing = True
    for prev, cur in zip(reversed(years[:-1]), reversed(years[1:]), strict=True):
        if annual[prev] <= 0 or annual[cur] < annual[prev] * (1 - CUT_TOLERANCE):
            break
        no_cut += 1
        if growing and annual[cur] > annual[prev]:
            growth += 1
        else:
            growing = False
    return no_cut, growth


class TtmIndex:
    """Dividendo de los últimos 12 meses en cualquier fecha, en O(log n)."""

    def __init__(self, regular: list[Payment]):
        self.days = [d for d, _ in regular]
        self.cumulative = [0.0, *accumulate(a for _, a in regular)]
        self.first = self.days[0] if self.days else None

    def _window(self, day: date) -> float:
        hi = bisect_right(self.days, day)
        lo = bisect_right(self.days, day - timedelta(days=365))
        return self.cumulative[hi] - self.cumulative[lo]

    def at(self, day: date) -> float:
        return max(self._window(day), self._window(day - TTM_GRACE))


def analyze(payments: list[Payment], today: date) -> DividendStats:
    regular, specials = split_specials(payments)
    annual = annual_totals(regular, today)
    no_cut, growth = streaks(annual)
    return DividendStats(
        regular=regular,
        specials=specials,
        annual=annual,
        dividend_ttm=TtmIndex(regular).at(today),
        dividend_last_year=annual[max(annual)] if annual else None,
        dgr_5y=cagr(annual, 5),
        dgr_10y=cagr(annual, 10),
        years_no_cut=no_cut,
        years_growth=growth,
    )


def yield_band(
    regular: list[Payment], closes: list[tuple[date, float]], today: date, years: int
) -> tuple[float | None, float | None]:
    """(yield media, percentil 80) de la yield semanal en los últimos `years` años."""
    index = TtmIndex(regular)
    if index.first is None:
        return None, None
    # Solo semanas con 12 meses completos de historia de dividendos
    start = max(today - timedelta(days=round(365.25 * years)), index.first + timedelta(days=365))
    days = [d for d, _ in closes]
    values = [
        index.at(day) / close
        for day, close in closes[bisect_left(days, start) :]
        if close > 0 and day <= today
    ]
    if len(values) < MIN_BAND_WEEKS:
        return None, None
    return fmean(values), quantiles(values, n=5)[-1]
