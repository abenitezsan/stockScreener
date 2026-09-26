"""Consultas del screener (nivel 1) y de la lista de seguimiento (nivel 2)."""

from dataclasses import dataclass, field

from sqlalchemy import Select, and_, case, func, literal, or_, select
from sqlalchemy.orm import Session

from app.analysis.valuation import buy_price, max_price_for_target, signal
from app.config import get_settings
from app.models import Fundamentals, Quote, Security, Valuation, WatchlistItem

S, Q, F, V = Security, Quote, Fundamentals, Valuation


def _ratio(num, den):
    return case((den > 0, num / den), else_=None)


price = Q.price
yield_ttm = _ratio(V.dividend_ttm, price)
yield_fwd = _ratio(V.dividend_forward, price)

# clave -> (etiqueta, expresión, formato). Formatos: text, money, pct, x (múltiplo), int, cap
COLUMNS = {
    "symbol": ("Ticker", S.symbol, "text"),
    "name": ("Empresa", S.name, "text"),
    "sector": ("Sector", S.sector, "text"),
    "country": ("País", S.country, "text"),
    "currency": ("Div.", S.currency, "text"),
    "price": ("Precio", price, "money"),
    "yield_ttm": ("Yield", yield_ttm, "pct"),
    "yield_fwd": ("Yield est.", yield_fwd, "pct"),
    "yield_avg_5y": ("Yield media 5a", V.yield_avg_5y, "pct"),
    "pe_ttm": ("PER", _ratio(price, F.eps_ttm), "x"),
    "pe_fwd": ("PER est.", _ratio(price, F.eps_forward), "x"),
    "payout": ("Payout BPA", F.payout_ratio, "pct"),
    "payout_fcf": ("Payout FCF", V.payout_fcf, "pct"),
    "dgr_5y": ("Crec. div. 5a", V.dgr_5y, "pct"),
    "dgr_10y": ("Crec. div. 10a", V.dgr_10y, "pct"),
    "years_no_cut": ("Años sin recorte", V.years_no_cut, "int"),
    "chowder": ("Chowder", yield_ttm + V.dgr_5y, "pct"),
    "total_return": ("Rent. total est.", yield_fwd + V.growth_used, "pct"),
    "market_cap_eur": ("Capitalización", F.market_cap_eur, "cap"),
    "fair_value": ("Precio justo", V.fair_value, "money"),
    "buy_price": ("Precio compra", V.buy_price, "money"),
    "upside": ("Potencial", _ratio(V.fair_value, price) - 1, "pct"),
}

SIGNAL = case(
    (or_(price.is_(None), V.buy_price.is_(None), V.fair_value.is_(None)), literal("none")),
    (and_(price <= V.buy_price, V.quality_ok.is_(True)), literal("green")),
    (price <= V.fair_value, literal("yellow")),
    else_=literal("red"),
)

SCREENER_COLUMNS = [
    "symbol",
    "name",
    "country",
    "price",
    "yield_ttm",
    "yield_fwd",
    "yield_avg_5y",
    "pe_ttm",
    "pe_fwd",
    "payout",
    "payout_fcf",
    "dgr_5y",
    "years_no_cut",
    "chowder",
    "total_return",
    "fair_value",
    "buy_price",
    "upside",
]
REGIONS = {"US": "EE. UU.", "CA": "Canadá", "EU": "Europa"}


@dataclass
class Filters:
    sector: str = ""
    regions: list[str] = field(default_factory=list)
    q: str = ""
    # En %, como se escriben en el formulario
    yield_avg_min: float | None = 3.0
    yield_avg_max: float | None = 6.0
    yield_min: float | None = None
    yield_max: float | None = None
    payout_max: float | None = None
    dgr_min: float | None = None
    years_no_cut_min: int | None = None
    cap_min_bn: float | None = None  # miles de millones de EUR
    quality_only: bool = False
    signal: str = ""
    sort: str = "yield_ttm"
    desc: bool = True


def _base_query(columns: list[str]) -> Select:
    return (
        select(
            S.id.label("id"),
            *(COLUMNS[c][1].label(c) for c in columns),
            SIGNAL.label("signal"),
            V.quality_ok.label("quality_ok"),
            V.flags.label("flags"),
        )
        .select_from(S)
        .outerjoin(Q, Q.security_id == S.id)
        .outerjoin(F, F.security_id == S.id)
        .outerjoin(V, V.security_id == S.id)
    )


def _apply_filters(query: Select, f: Filters, with_sector: bool = True) -> Select:
    conditions = [S.active]
    ranges = [
        (V.yield_avg_5y, f.yield_avg_min, f.yield_avg_max, 100),
        (yield_ttm, f.yield_min, f.yield_max, 100),
        (F.payout_ratio, None, f.payout_max, 100),
        (V.dgr_5y, f.dgr_min, None, 100),
        (V.years_no_cut, f.years_no_cut_min, None, 1),
        (F.market_cap_eur, f.cap_min_bn, None, 1e-9),
    ]
    for expr, low, high, scale in ranges:
        if low is not None:
            conditions.append(expr >= low / scale)
        if high is not None:
            conditions.append(expr <= high / scale)
    if with_sector and f.sector:
        conditions.append(S.sector == f.sector)
    if f.regions:
        conditions.append(S.region.in_(f.regions))
    if f.q:
        pattern = f"%{f.q}%"
        conditions.append(or_(S.symbol.ilike(pattern), S.name.ilike(pattern)))
    if f.quality_only:
        conditions.append(V.quality_ok.is_(True))
    if f.signal:
        conditions.append(SIGNAL == f.signal)
    return query.where(*conditions)


def screen(session: Session, f: Filters, limit: int = 500) -> list:
    sort = COLUMNS.get(f.sort, COLUMNS["yield_ttm"])[1]
    order = sort.desc().nulls_last() if f.desc else sort.asc().nulls_last()
    query = _apply_filters(_base_query(SCREENER_COLUMNS), f).order_by(order, S.symbol)
    return list(session.execute(query.limit(limit)))


def sector_counts(session: Session, f: Filters) -> list[tuple[str, int]]:
    """Número de resultados por sector con el resto de filtros aplicados (pestañas)."""
    query = _apply_filters(
        select(S.sector, func.count())
        .select_from(S)
        .outerjoin(Q, Q.security_id == S.id)
        .outerjoin(F, F.security_id == S.id)
        .outerjoin(V, V.security_id == S.id),
        f,
        with_sector=False,
    )
    rows = session.execute(query.where(S.sector.is_not(None)).group_by(S.sector))
    return sorted(rows, key=lambda r: (-r[1], r[0]))


def watchlist_ids(session: Session) -> set[int]:
    return set(session.scalars(select(WatchlistItem.security_id)))


# --- Nivel 2 -----------------------------------------------------------------------


@dataclass
class WatchRow:
    security: Security
    quote: Quote | None
    fundamentals: Fundamentals | None
    valuation: Valuation | None
    item: WatchlistItem
    margin_of_safety: float
    target_total_return: float
    max_price_target: float | None
    buy_price: float | None
    signal: str

    @property
    def price(self) -> float | None:
        return self.quote.price if self.quote else None

    def ratio(self, value: float | None) -> float | None:
        return value / self.price if value and self.price else None

    @property
    def range_52w(self) -> float | None:
        """Posición en el rango de 52 semanas: 0 = mínimo, 1 = máximo."""
        f = self.fundamentals
        if not (f and f.week52_high and f.week52_low and self.price):
            return None
        span = f.week52_high - f.week52_low
        return (self.price - f.week52_low) / span if span > 0 else None

    @property
    def vs_ma200(self) -> float | None:
        f = self.fundamentals
        return self.price / f.ma200 - 1 if f and f.ma200 and self.price else None


def watch_row(session: Session, item: WatchlistItem) -> WatchRow:
    settings = get_settings()
    sec = session.get(Security, item.security_id)
    quote = session.get(Quote, sec.id)
    v = session.get(Valuation, sec.id)
    mos = item.margin_of_safety if item.margin_of_safety is not None else settings.margin_of_safety
    target = (
        item.target_total_return
        if item.target_total_return is not None
        else settings.target_total_return
    )
    ceiling = buy = None
    if v:
        ceiling = max_price_for_target(v.dividend_forward, v.growth_used, target)
        buy = buy_price(v.fair_value, mos, v.dividend_forward, v.growth_used, target)
    return WatchRow(
        security=sec,
        quote=quote,
        fundamentals=session.get(Fundamentals, sec.id),
        valuation=v,
        item=item,
        margin_of_safety=mos,
        target_total_return=target,
        max_price_target=ceiling,
        buy_price=buy,
        signal=signal(
            quote.price if quote else None,
            buy,
            v.fair_value if v else None,
            v.quality_ok if v else None,
        ),
    )


def watchlist(session: Session) -> list[WatchRow]:
    """Primero las que están en zona de compra; dentro de cada color, las más cercanas a ella."""
    order = {"green": 0, "yellow": 1, "red": 2, "none": 3}
    rows = [watch_row(session, item) for item in session.scalars(select(WatchlistItem))]
    return sorted(rows, key=lambda r: (order[r.signal], -(r.ratio(r.buy_price) or 0)))
