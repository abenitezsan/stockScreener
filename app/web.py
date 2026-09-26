"""Rutas web (HTML generado en el servidor + HTMX)."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import Undefined
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import screener
from app.analysis.dividends import TtmIndex, analyze
from app.config import get_settings, today
from app.db import get_session
from app.models import DividendEvent, PriceHistory, Security, WatchlistItem

DbSession = Annotated[Session, Depends(get_session)]
router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

FLAG_LABELS = {
    "payout_alto": "Payout sobre beneficios alto",
    "payout_fcf_alto": "Payout sobre FCF alto",
    "payout_ocf_alto": "Payout sobre flujo operativo alto",
    "fcf_negativo": "FCF negativo",
    "bpa_negativo": "BPA negativo",
    "sin_datos_payout": "Faltan datos de payout",
    "sin_dividendo": "No paga dividendo",
    "pocos_datos": "Menos de 3 años de dividendos",
    "recorte_reciente": "Recorte en los últimos 5 años",
    "yield_anomala": "Yield > 1,5× su media (¿trampa?)",
    "dividendos_reescalados": "Dividendos corregidos ×100 (unidades)",
}
GROUP_LABELS = {
    "general": "General",
    "utilities": "Utilities y telecos",
    "reit": "REIT",
    "financials": "Financieras",
    "cyclical": "Cíclicas",
}
SIGNAL_LABELS = {
    "green": ("●", "Compra"),
    "yellow": ("◐", "Vigilar"),
    "red": ("○", "Cara"),
    "none": ("–", "Sin datos"),
}


def _fmt_number(value, digits: int = 2) -> str:
    if value is None:
        return "–"
    return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")


def fmt(value, kind: str = "money") -> str:
    # `x if cond` sin else en Jinja da Undefined: se muestra como dato ausente
    if value is None or isinstance(value, Undefined):
        return "–"
    match kind:
        case "pct":
            return _fmt_number(value * 100, 1) + " %"
        case "x":
            return _fmt_number(value, 1)
        case "int":
            return str(int(value))
        case "cap":
            if value >= 1e9:
                return _fmt_number(value / 1e9, 1) + " mil M€"
            return _fmt_number(value / 1e6, 0) + " M€"
        case "text":
            return str(value)
        case _:
            return _fmt_number(value, 2)


templates.env.filters["fmt"] = fmt
templates.env.globals.update(
    FLAG_LABELS=FLAG_LABELS,
    GROUP_LABELS=GROUP_LABELS,
    SIGNAL_LABELS=SIGNAL_LABELS,
    COLUMNS=screener.COLUMNS,
    REGIONS=screener.REGIONS,
)


# --- Filtros desde la URL ------------------------------------------------------------


def _float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def parse_filters(request: Request) -> screener.Filters:
    p = request.query_params
    f = screener.Filters()
    if "submitted" not in p:  # primera carga: valores por defecto
        f.sector = p.get("sector", "")
        return f
    f.sector = p.get("sector", "")
    f.regions = [r for r in p.getlist("region") if r in screener.REGIONS]
    f.q = p.get("q", "").strip()
    f.yield_avg_min = _float(p.get("yield_avg_min"))
    f.yield_avg_max = _float(p.get("yield_avg_max"))
    f.yield_min = _float(p.get("yield_min"))
    f.yield_max = _float(p.get("yield_max"))
    f.payout_max = _float(p.get("payout_max"))
    f.dgr_min = _float(p.get("dgr_min"))
    years = _float(p.get("years_no_cut_min"))
    f.years_no_cut_min = int(years) if years is not None else None
    f.cap_min_bn = _float(p.get("cap_min_bn"))
    f.quality_only = p.get("quality_only") == "1"
    f.signal = p.get("signal", "") if p.get("signal") in SIGNAL_LABELS else ""
    f.sort = p.get("sort", "yield_ttm") if p.get("sort") in screener.COLUMNS else "yield_ttm"
    f.desc = p.get("desc", "1") == "1"
    return f


def _screener_context(session: Session, f: screener.Filters) -> dict:
    return {
        "filters": f,
        "rows": screener.screen(session, f),
        "sectors": screener.sector_counts(session, f),
        "columns": screener.SCREENER_COLUMNS,
        "watched": screener.watchlist_ids(session),
    }


# --- Rutas ---------------------------------------------------------------------------


@router.get("/")
def index():
    return RedirectResponse("/screener")


@router.get("/screener", response_class=HTMLResponse)
def screener_page(request: Request, session: DbSession):
    ctx = _screener_context(session, parse_filters(request))
    # Con HTMX (cambio de filtros) solo se devuelve la tabla
    partial = request.headers.get("HX-Request") and not request.headers.get(
        "HX-History-Restore-Request"
    )
    template = "_screener_results.html" if partial else "screener.html"
    return templates.TemplateResponse(request, template, ctx)


@router.post("/watchlist/{security_id}/toggle", response_class=HTMLResponse)
def toggle_watch(request: Request, security_id: int, session: DbSession):
    if session.get(Security, security_id) is None:
        raise HTTPException(404)
    item = session.get(WatchlistItem, security_id)
    if item:
        session.delete(item)
    else:
        session.add(WatchlistItem(security_id=security_id))
    session.commit()
    return templates.TemplateResponse(
        request, "_watch_button.html", {"id": security_id, "watched": item is None}
    )


@router.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request, session: DbSession):
    return templates.TemplateResponse(
        request, "watchlist.html", {"rows": screener.watchlist(session)}
    )


@router.get("/security/{symbol}", response_class=HTMLResponse)
def security_page(request: Request, symbol: str, session: DbSession):
    sec = session.scalar(select(Security).where(Security.symbol == symbol.upper()))
    if sec is None:
        raise HTTPException(404, "Valor no encontrado")
    item = session.get(WatchlistItem, sec.id) or WatchlistItem(security_id=sec.id)
    row = screener.watch_row(session, item)
    closes = session.execute(
        select(PriceHistory.day, PriceHistory.close)
        .where(PriceHistory.security_id == sec.id)
        .order_by(PriceHistory.day)
    ).all()
    dividends = session.execute(
        select(DividendEvent.ex_date, DividendEvent.amount)
        .where(DividendEvent.security_id == sec.id)
        .order_by(DividendEvent.ex_date)
    ).all()
    return templates.TemplateResponse(
        request,
        "security.html",
        {
            "row": row,
            "sec": sec,
            "watched": session.get(WatchlistItem, sec.id) is not None,
            "settings": get_settings(),
            "chart_data": _chart_data(row, closes, dividends),
        },
    )


def _chart_data(row: screener.WatchRow, closes, dividends) -> dict:
    """Series para los gráficos: precio 2 años, yield 10 años y dividendo por año."""
    stats = analyze([(d, a) for d, a in dividends], today())
    index = TtmIndex(stats.regular)
    recent = closes[-105:]  # ~2 años de cierres semanales
    first_full = index.first and (index.first.toordinal() + 365)
    yields = [
        (d.isoformat(), round(index.at(d) / c * 100, 3))
        for d, c in closes
        if c > 0 and first_full and d.toordinal() >= first_full
    ]
    v = row.valuation
    return {
        "price": [(d.isoformat(), round(c, 4)) for d, c in recent],
        "fair_value": v.fair_value if v else None,
        "buy_price": row.buy_price,
        "yield": yields,
        "yield_avg": v.yield_avg_5y * 100 if v and v.yield_avg_5y else None,
        "yield_p80": v.yield_p80_5y * 100 if v and v.yield_p80_5y else None,
        "annual": [(str(y), round(a, 4)) for y, a in stats.annual.items()],
        "currency": row.security.currency or "",
    }


@router.post("/security/{symbol}/watch")
def update_watch(
    symbol: str,
    session: DbSession,
    notes: Annotated[str, Form()] = "",
    margin_of_safety: Annotated[str, Form()] = "",
    target_total_return: Annotated[str, Form()] = "",
):
    sec = session.scalar(select(Security).where(Security.symbol == symbol.upper()))
    if sec is None:
        raise HTTPException(404)
    item = session.get(WatchlistItem, sec.id) or WatchlistItem(security_id=sec.id)
    item.notes = notes.strip() or None
    mos, target = _float(margin_of_safety), _float(target_total_return)
    item.margin_of_safety = mos / 100 if mos is not None else None
    item.target_total_return = target / 100 if target is not None else None
    session.add(item)
    session.commit()
    return RedirectResponse(f"/security/{sec.symbol}", status_code=303)


@router.get("/portfolio", response_class=HTMLResponse)
def portfolio_page(request: Request):
    return templates.TemplateResponse(request, "portfolio.html")
