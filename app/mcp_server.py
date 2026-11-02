"""Servidor MCP (Model Context Protocol) para Claude: transporte «Streamable HTTP» sin estado.

Cada petición es un POST a `/mcp` con un mensaje JSON-RPC 2.0 (o una lista) y la respuesta es
JSON (sin flujo SSE, que el protocolo permite). Solo se exponen herramientas (`tools`):
consultan el screener, las fichas, el seguimiento y la cartera del usuario dueño del token, y
permiten guardar análisis. Cada llamada queda en el historial (`McpCall`).

La autorización (OAuth 2.1 o clave personal) está en `app/oauth.py`.
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import portfolio, screener
from app.analysis.dividends import analyze
from app.config import get_settings, today
from app.db import SessionLocal
from app.models import (
    AiNote,
    DividendEvent,
    DividendPayment,
    FinancialsAnnual,
    McpCall,
    Security,
    Transaction,
    User,
    WatchlistItem,
)
from app.oauth import Caller, caller_for, public_base

log = logging.getLogger(__name__)
router = APIRouter()

SERVER_NAME = "stockscreener"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
MAX_RESULT_CHARS = 120_000  # Claude admite unos 150 000 caracteres por resultado

INSTRUCTIONS = """\
Datos de la app Stock Screener del usuario: un screener de acciones para inversión por \
dividendos (EE. UU., Canadá y Europa, el universo comprable en HeyTrade), su lista de \
seguimiento y su cartera privada.

- Los importes de la cartera están en EUR; los precios y dividendos por acción, en la divisa \
del valor (campo `divisa`).
- Los porcentajes llevan el sufijo `_pct` y ya están multiplicados por 100 (4.5 = 4,5 %).
- El semáforo: `compra` (precio <= precio de compra y dividendo sostenible), `vigilar` (por \
debajo del precio justo), `cara` (por encima) o `sin_datos`.
- Los tickers son los de Yahoo: AAPL, SAN.MC, ENB.TO, ULVR.L, SAP.DE…
- Si el usuario quiere conservar una conclusión, guárdala con `guardar_analisis`; \
`historial_analisis` muestra las anteriores.
- Es información para el propio inversor, no una recomendación profesional.
"""

SIGNALS = {"green": "compra", "yellow": "vigilar", "red": "cara", "none": "sin_datos"}
SIGNAL_IN = {v: k for k, v in SIGNALS.items()}


class ToolError(Exception):
    """Error que se devuelve a Claude como resultado de la herramienta (isError)."""


# --- Formato ---------------------------------------------------------------------------


def num(value, digits: int = 2):
    if value is None:
        return None
    return round(float(value), digits)


def pct(value, digits: int = 2):
    return None if value is None else round(float(value) * 100, digits)


def day(value: date | datetime | None) -> str | None:
    return value.isoformat() if value else None


def _clean(obj):
    """Quita claves con None (resultados más cortos) y convierte Decimal y fechas."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if v is not None and v != [] and v != {}}
    if isinstance(obj, list | tuple):
        return [_clean(v) for v in obj]
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, date | datetime):
        return obj.isoformat()
    return obj


def _find(session: Session, ident: str) -> Security:
    ident = (ident or "").strip()
    if not ident:
        raise ToolError("Falta el ticker")
    sec = portfolio.find_security(session, ident)
    if sec is None:
        sec = session.scalar(select(Security).where(Security.symbol == ident.upper()))
    if sec is None:
        raise ToolError(
            f"No encuentro «{ident}» en el universo. Usa buscar_valor para ver tickers parecidos."
        )
    return sec


# --- Argumentos ------------------------------------------------------------------------


def _str(args: dict, key: str, default: str = "", max_len: int = 200) -> str:
    value = args.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ToolError(f"«{key}» debe ser texto")
    return value.strip()[:max_len]


def _num(args: dict, key: str) -> float | None:
    value = args.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ToolError(f"«{key}» debe ser un número")
    try:
        return float(value)
    except ValueError:
        raise ToolError(f"«{key}» debe ser un número") from None


def _int(args: dict, key: str, default: int, low: int, high: int) -> int:
    value = _num(args, key)
    return default if value is None else max(low, min(high, int(value)))


def _bool(args: dict, key: str, default: bool) -> bool:
    value = args.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "si", "sí", "yes")
    return bool(value)


# --- Herramientas: mercado -------------------------------------------------------------


def _screen_row(row) -> dict:
    return {
        "ticker": row.symbol,
        "nombre": row.name,
        "sector": row.sector,
        "pais": row.country,
        "precio": num(row.price, 4),
        "yield_pct": pct(row.yield_ttm),
        "yield_est_pct": pct(row.yield_fwd),
        "yield_media_5a_pct": pct(row.yield_avg_5y),
        "per": num(row.pe_ttm, 1),
        "per_est": num(row.pe_fwd, 1),
        "payout_bpa_pct": pct(row.payout, 1),
        "payout_fcf_pct": pct(row.payout_fcf, 1),
        "crec_div_5a_pct": pct(row.dgr_5y),
        "crec_div_10a_pct": pct(getattr(row, "dgr_10y", None)),
        "anios_sin_recorte": row.years_no_cut,
        "chowder_pct": pct(row.chowder),
        "rent_total_est_pct": pct(row.total_return),
        "precio_justo": num(row.fair_value, 4),
        "precio_compra": num(row.buy_price, 4),
        "potencial_pct": pct(row.upside, 1),
        "semaforo": SIGNALS.get(row.signal, row.signal),
        "dividendo_sostenible": row.quality_ok,
    }


def tool_buscar_valor(session: Session, user: User, args: dict) -> dict:
    text = _str(args, "texto", max_len=60)
    if len(text) < 1:
        raise ToolError("Escribe parte del ticker, del nombre o el ISIN")
    pattern = f"%{text}%"
    rows = session.execute(
        screener._base_query(screener.SCREENER_COLUMNS)
        .where(
            Security.active,
            or_(
                Security.symbol.ilike(pattern),
                Security.name.ilike(pattern),
                Security.isin == text.upper(),
            ),
        )
        .order_by(screener.F.market_cap_eur.desc().nulls_last())
        .limit(25)
    ).all()
    held = portfolio.held_security_ids(session, user.id)
    watched = screener.watchlist_ids(session, user.id)
    return {
        "resultados": [
            {
                **_screen_row(r),
                "en_cartera": r.id in held or None,
                "en_seguimiento": r.id in watched or None,
            }
            for r in rows
        ]
    }


def tool_screener(session: Session, user: User, args: dict) -> dict:
    regions = args.get("regiones") or []
    if isinstance(regions, str):
        regions = [regions]
    regions = [r.upper() for r in regions if isinstance(r, str) and r.upper() in screener.REGIONS]
    sort = _str(args, "orden", "yield_ttm")
    if sort not in screener.COLUMNS:
        raise ToolError(f"«orden» debe ser una de: {', '.join(screener.SCREENER_COLUMNS)}")
    signal = _str(args, "semaforo")
    if signal and signal not in SIGNAL_IN:
        raise ToolError("«semaforo» debe ser compra, vigilar o cara")
    f = screener.Filters(
        sector=_str(args, "sector"),
        regions=regions,
        q=_str(args, "texto", max_len=60),
        yield_avg_min=_num(args, "yield_media_5a_min"),
        yield_min=_num(args, "yield_min"),
        yield_max=_num(args, "yield_max"),
        payout_max=_num(args, "payout_max"),
        dgr_min=_num(args, "crec_div_5a_min"),
        years_no_cut_min=(
            int(_num(args, "anios_sin_recorte_min"))
            if _num(args, "anios_sin_recorte_min") is not None
            else None
        ),
        cap_min_bn=_num(args, "capitalizacion_min_mil_millones"),
        dividend_only=_bool(args, "solo_con_dividendo", True),
        quality_only=_bool(args, "solo_sostenibles", False),
        signal=SIGNAL_IN.get(signal, ""),
        sort=sort,
        desc=_bool(args, "descendente", True),
    )
    limit = _int(args, "limite", 30, 1, 100)
    rows = screener.screen(session, f, limit=limit + 1)
    held = portfolio.held_security_ids(session, user.id)
    watched = screener.watchlist_ids(session, user.id)
    sectors = screener.sector_counts(session, f)
    return {
        "total_por_sector": {name: count for name, count in sectors},
        "hay_mas": len(rows) > limit or None,
        "resultados": [
            {
                **_screen_row(r),
                "en_cartera": r.id in held or None,
                "en_seguimiento": r.id in watched or None,
            }
            for r in rows[:limit]
        ],
    }


def tool_ficha_valor(session: Session, user: User, args: dict) -> dict:
    from app.web import FLAG_LABELS, hard_flag_reasons

    sec = _find(session, _str(args, "ticker", max_len=32))
    item = screener.watch_item(session, user.id, sec.id)
    row = screener.watch_row(session, item or WatchlistItem(security_id=sec.id))
    v, f, q = row.valuation, row.fundamentals, row.quote
    price = row.price
    events = session.execute(
        select(DividendEvent.ex_date, DividendEvent.pay_date, DividendEvent.amount)
        .where(DividendEvent.security_id == sec.id)
        .order_by(DividendEvent.ex_date)
    ).all()
    stats = analyze([(e.ex_date, e.amount) for e in events], today())
    accounts = session.scalars(
        select(FinancialsAnnual)
        .where(FinancialsAnnual.security_id == sec.id)
        .order_by(FinancialsAnnual.period_end.desc())
        .limit(5)
    ).all()
    position = portfolio.position_for(session, user.id, sec.id)
    notes = session.scalars(
        select(AiNote)
        .where(AiNote.user_id == user.id, AiNote.security_id == sec.id)
        .order_by(AiNote.created_at.desc())
        .limit(10)
    ).all()
    next_div = portfolio._next_dividend(session, sec.id, today())
    return {
        "ticker": sec.symbol,
        "nombre": sec.name,
        "isin": sec.isin,
        "pais": sec.country,
        "region": sec.region,
        "bolsa": sec.exchange,
        "sector": sec.sector,
        "industria": sec.industry,
        "grupo_sostenibilidad": sec.sector_group,
        "divisa": sec.currency,
        "divisa_cuentas": sec.financial_currency,
        "indices": sec.universes,
        "cotizacion": {
            "precio": num(price, 4),
            "cierre_anterior": num(q.previous_close, 4) if q else None,
            "fecha": day(q.as_of) if q else None,
        },
        "dividendo": {
            "por_accion_12m": num(v.dividend_ttm, 4) if v else None,
            "por_accion_estimado": num(v.dividend_forward, 4) if v else None,
            "por_accion_ultimo_anio": num(v.dividend_last_year, 4) if v else None,
            "yield_pct": pct(row.ratio(v.dividend_ttm)) if v else None,
            "yield_est_pct": pct(row.ratio(v.dividend_forward)) if v else None,
            "yield_media_5a_pct": pct(v.yield_avg_5y) if v else None,
            "yield_p80_5a_pct": pct(v.yield_p80_5y) if v else None,
            "yield_media_10a_pct": pct(v.yield_avg_10y) if v else None,
            "crec_5a_pct": pct(v.dgr_5y) if v else None,
            "crec_10a_pct": pct(v.dgr_10y) if v else None,
            "crec_usado_valoracion_pct": pct(v.growth_used) if v else None,
            "anios_sin_recorte": v.years_no_cut if v else None,
            "anios_creciendo": v.years_growth if v else None,
            "payout_bpa_pct": pct(f.payout_ratio, 1) if f else None,
            "payout_fcf_pct": pct(v.payout_fcf, 1) if v else None,
            "payout_flujo_operativo_pct": pct(v.payout_ocf, 1) if v else None,
            "proximo_ex_dividendo": (
                {"fecha": day(next_div.ex_date), "estimada": next_div.estimated}
                if next_div
                else None
            ),
            "por_anio": {str(y): num(a, 4) for y, a in stats.annual.items()},
            "ultimos_pagos": [
                {"ex": day(e.ex_date), "pago": day(e.pay_date), "importe": num(e.amount, 4)}
                for e in events[-12:]
            ],
        },
        "valoracion": {
            "semaforo": SIGNALS.get(row.signal, row.signal),
            "precio_justo": num(v.fair_value, 4) if v else None,
            "precio_compra": num(row.buy_price, 4),
            "precio_max_para_rent_objetivo": num(row.max_price_target, 4),
            "potencial_pct": pct(row.ratio(v.fair_value) - 1, 1)
            if v and row.ratio(v.fair_value)
            else None,
            "margen_seguridad_pct": pct(row.margin_of_safety),
            "rent_total_objetivo_pct": pct(row.target_total_return),
            "rent_total_est_pct": (
                pct(row.ratio(v.dividend_forward) + v.growth_used)
                if v and row.ratio(v.dividend_forward) is not None and v.growth_used is not None
                else None
            ),
            "metodos": {
                "por_yield_media": num(v.fv_yield, 4),
                "precio_compra_por_yield": num(v.buy_yield, 4),
                "por_per_medio": num(v.fv_pe, 4),
                "per_medio": num(v.pe_avg, 1),
                "anios_per": v.pe_years,
                "chowder": num(v.fv_chowder, 4),
                "umbral_chowder_pct": pct(v.chowder_threshold),
                "gordon": num(v.fv_gordon, 4),
            }
            if v
            else None,
            "dividendo_sostenible": v.quality_ok if v else None,
            "alertas": [FLAG_LABELS.get(x, x) for x in (v.flags if v else [])],
            "motivos_no_sostenible": [
                {"motivo": label, "detalle": text}
                for label, text in hard_flag_reasons(session, sec, row)
            ],
            "calculada": day(v.computed_at) if v else None,
        },
        "fundamentales": {
            "capitalizacion_eur": num(f.market_cap_eur, 0),
            "per": num(f.pe_ttm, 1),
            "per_est": num(f.pe_forward, 1),
            "bpa": num(f.eps_ttm, 4),
            "bpa_est": num(f.eps_forward, 4),
            "precio_valor_contable": num(f.price_to_book, 2),
            "ev_ebitda": num(f.ev_to_ebitda, 1),
            "roe_pct": pct(f.roe, 1),
            "margen_neto_pct": pct(f.profit_margin, 1),
            "crec_ingresos_pct": pct(f.revenue_growth, 1),
            "crec_beneficio_pct": pct(f.earnings_growth, 1),
            "deuda_patrimonio_pct": num(f.debt_to_equity, 1),
            "beta": num(f.beta, 2),
            "max_52s": num(f.week52_high, 4),
            "min_52s": num(f.week52_low, 4),
            "posicion_rango_52s_pct": pct(row.range_52w, 0),
            "media_200d": num(f.ma200, 4),
            "vs_media_200d_pct": pct(row.vs_ma200, 1),
            "precio_objetivo_analistas": num(f.target_mean_price, 4),
        }
        if f
        else None,
        "cuentas_anuales": [
            {
                "cierre": day(a.period_end),
                "ingresos": num(a.revenue, 0),
                "beneficio_neto": num(a.net_income, 0),
                "bpa": num(a.eps, 4),
                "flujo_operativo": num(a.operating_cashflow, 0),
                "capex": num(a.capex, 0),
                "fcf": num(a.free_cashflow, 0),
                "dividendos_pagados": num(a.dividends_paid, 0),
                "acciones": num(a.shares, 0),
            }
            for a in accounts
        ],
        "seguimiento": {
            "en_seguimiento": item is not None,
            "notas": item.notes if item else None,
        },
        "mi_posicion": _position(position) if position else None,
        "analisis_guardados": [
            {"id": n.id, "titulo": n.title, "fecha": day(n.created_at)} for n in notes
        ],
    }


# --- Herramientas: cartera y seguimiento -------------------------------------------------


def _position(p: portfolio.Position, total_value: float | None = None) -> dict:
    last, nxt = p.last_dividend, p.next_dividend
    return {
        "ticker": p.security.symbol,
        "nombre": p.security.name,
        "sector": p.security.sector,
        "pais": p.security.country,
        "divisa": p.security.currency,
        "abierta": p.is_open,
        "acciones": num(p.shares, 6),
        "coste_medio_eur": num(p.avg_cost_eur, 4),
        "coste_eur": num(p.cost_eur),
        "precio": num(p.price, 4),
        "cambio_eur_por_unidad": num(p.fx, 6),
        "valor_eur": num(p.value_eur),
        "peso_pct": pct(p.value_eur / total_value, 1) if total_value and p.value_eur else None,
        "plusvalia_eur": num(p.unrealized_eur),
        "plusvalia_pct": pct(p.unrealized_pct),
        "efecto_divisa_eur": num(p.fx_total_eur),
        "cambio_hoy_eur": num(p.day_change_eur),
        "cambio_hoy_pct": pct(p.day_change_pct),
        "invertido_eur": num(p.invested_eur),
        "ventas_eur": num(p.sold_eur),
        "realizado_eur": num(p.realized_eur),
        "dividendos_cobrados_bruto_eur": num(p.dividends_gross_eur),
        "dividendos_cobrados_neto_eur": num(p.dividends_net_eur),
        "rent_total_eur": num(p.total_return_eur),
        "rent_total_pct": pct(p.total_return_pct),
        "dividendo_anual_por_accion": num(p.annual_dividend_ps, 4),
        "dividendo_anual_eur": num(p.annual_dividend_eur),
        "yield_pct": pct(p.current_yield),
        "yield_on_cost_pct": pct(p.yoc),
        "ultimo_dividendo": {
            "ex": day(last.ex_date),
            "importe": num(last.amount, 4),
            "vs_anio_anterior_pct": pct(last.change, 1),
        }
        if last
        else None,
        "proximo_ex_dividendo": {"fecha": day(nxt.ex_date), "estimada": nxt.estimated}
        if nxt
        else None,
    }


def tool_cartera(session: Session, user: User, args: dict) -> dict:
    rows = portfolio.positions(session, user.id, include_closed=True)
    open_rows = [p for p in rows if p.is_open]
    s = portfolio.summarize(rows)
    invested = float(portfolio.invested_total(session, user.id, today()))
    result = {
        "fecha": day(today()),
        "divisa": get_settings().base_currency,
        "resumen": {
            "valor_eur": num(s.value_eur),
            "coste_eur": num(s.cost_eur),
            "plusvalia_eur": num(s.unrealized_eur),
            "plusvalia_pct": pct(s.unrealized_pct),
            "cambio_hoy_eur": num(s.day_change_eur),
            "cambio_hoy_pct": pct(s.day_change_pct),
            "cambio_hoy_por_precio_eur": num(s.day_price_eur),
            "cambio_hoy_por_divisa_eur": num(s.day_fx_eur),
            "efecto_divisa_total_eur": num(s.fx_total_eur),
            "realizado_eur": num(s.realized_eur),
            "dividendos_cobrados_bruto_eur": num(s.dividends_gross_eur),
            "dividendos_cobrados_neto_eur": num(s.dividends_net_eur),
            "dividendos_anteriores_a_la_app_eur": num(
                portfolio.imported_dividends_eur(session, user.id)
            ),
            "rent_total_eur": num(s.total_return_eur),
            "rent_total_pct": pct(s.total_return_pct),
            "comprado_total_eur": num(s.invested_eur),
            "aportado_eur": num(invested) if invested else None,
            "ganancia_sobre_aportado_eur": num(s.value_eur - invested) if invested else None,
            "dividendo_anual_estimado_eur": num(s.annual_dividend_eur),
            "yield_pct": pct(s.yield_now),
            "yield_on_cost_pct": pct(s.yoc),
            "sin_cotizacion": s.missing_prices,
        },
        "divisas": [
            {
                "divisa": line.currency,
                "eur_por_unidad": num(line.rate, 6),
                "hoy_pct": pct(line.day_pct),
                "mes_pct": pct(line.month_pct),
                "valor_eur": num(line.value_eur),
                "efecto_total_eur": num(line.total_eur),
            }
            for line in s.fx_lines
        ],
        "reparto": {
            "por_sector": _alloc(portfolio.allocation(open_rows, lambda p: p.security.sector)),
            "por_pais": _alloc(portfolio.allocation(open_rows, lambda p: p.security.country)),
            "por_divisa": _alloc(portfolio.allocation(open_rows, lambda p: p.security.currency)),
        },
        "posiciones": [_position(p, s.value_eur) for p in open_rows],
    }
    if _bool(args, "incluir_cerradas", False):
        result["cerradas"] = [_position(p) for p in rows if not p.is_open]
    return result


def _alloc(pairs: list[tuple[str, float]]) -> list[dict]:
    total = sum(v for _, v in pairs) or 1.0
    return [{"grupo": k, "valor_eur": num(v), "peso_pct": pct(v / total, 1)} for k, v in pairs]


def tool_dividendos_cartera(session: Session, user: User, args: dict) -> dict:
    rows = portfolio.positions(session, user.id)
    proj = portfolio.project_dividends(session, rows)
    received = portfolio.tax_summary(session, user.id)
    return {
        "proyeccion_12_meses": {
            "total_bruto_eur": num(proj.total_eur),
            "por_mes": [{"mes": f"{y}-{m:02d}", "bruto_eur": num(v)} for y, m, v in proj.months],
            "repartidos_por_igual_sin_calendario": proj.estimated,
            "pagos": [
                {
                    "ticker": i.security.symbol,
                    "ex": day(i.ex_date),
                    "cobro": day(i.pay_date),
                    "bruto_eur": num(i.gross_eur),
                    "fecha_estimada": i.estimated or i.pay_estimated or None,
                }
                for i in proj.items
            ],
        },
        "cobrado_por_anio": [
            {
                "anio": y.year,
                "bruto_eur": num(y.gross_eur),
                "retencion_origen_eur": num(y.origin_eur),
                "retencion_destino_eur": num(y.domestic_eur),
                "gastos_eur": num(y.fees_eur),
                "neto_eur": num(y.net_eur),
                "cobros": len(y.rows),
            }
            for y in received
        ],
        "cobrados_antes_de_la_app_eur": num(portfolio.imported_dividends_eur(session, user.id)),
    }


def tool_fiscal(session: Session, user: User, args: dict) -> dict:
    year = _num(args, "anio")
    years = portfolio.tax_summary(session, user.id)
    out = []
    for y in years:
        if year is not None and y.year != int(year):
            continue
        entry = {
            "anio": y.year,
            "bruto_eur": num(y.gross_eur),
            "retencion_origen_eur": num(y.origin_eur),
            "retencion_destino_eur": num(y.domestic_eur),
            "gastos_eur": num(y.fees_eur),
            "neto_eur": num(y.net_eur),
            "por_pais": {
                c: {
                    "bruto_eur": num(v["gross"]),
                    "retencion_origen_eur": num(v["origin"]),
                    "retencion_destino_eur": num(v["domestic"]),
                }
                for c, v in y.by_country.items()
            },
        }
        if year is not None:
            entry["cobros"] = [
                {
                    "fecha_pago": day(r.payment.pay_date),
                    "ticker": r.security.symbol,
                    "pais": r.security.country,
                    "divisa": r.payment.currency,
                    "bruto_eur": num(r.gross_eur),
                    "retencion_origen_eur": num(r.origin_eur),
                    "retencion_destino_eur": num(r.domestic_eur),
                    "gastos_eur": num(r.fees_eur),
                    "neto_eur": num(r.net_eur),
                }
                for r in y.rows
            ]
        out.append(entry)
    return {
        "aviso": "Informativo: no sustituye al certificado fiscal del bróker.",
        "anios": out,
    }


def tool_operaciones(session: Session, user: User, args: dict) -> dict:
    limit = _int(args, "limite", 50, 1, 500)
    ticker = _str(args, "ticker", max_len=32)
    tx_query = (
        select(Transaction, Security)
        .join(Security, Security.id == Transaction.security_id)
        .where(Transaction.user_id == user.id)
    )
    div_query = (
        select(DividendPayment, Security)
        .join(Security, Security.id == DividendPayment.security_id)
        .where(DividendPayment.user_id == user.id)
    )
    if ticker:
        sec = _find(session, ticker)
        tx_query = tx_query.where(Transaction.security_id == sec.id)
        div_query = div_query.where(DividendPayment.security_id == sec.id)
    txs = session.execute(
        tx_query.order_by(Transaction.trade_date.desc(), Transaction.id.desc()).limit(limit)
    ).all()
    divs = session.execute(
        div_query.order_by(DividendPayment.pay_date.desc(), DividendPayment.id.desc()).limit(limit)
    ).all()
    return {
        "operaciones": [
            {
                "fecha": day(t.trade_date),
                "tipo": "compra" if t.kind == "buy" else "venta",
                "ticker": s.symbol,
                "acciones": num(t.quantity, 6),
                "precio": num(t.price, 4),
                "divisa": t.currency,
                "cambio": num(t.fx_rate, 6),
                "comisiones_eur": num(t.fees),
                "total_eur": num(t.total_eur),
                "origen": t.source,
                "notas": t.notes,
            }
            for t, s in txs
        ],
        "dividendos_cobrados": [
            {
                "fecha_pago": day(d.pay_date),
                "ex": day(d.ex_date),
                "ticker": s.symbol,
                "acciones": num(d.shares, 6),
                "por_accion": num(d.per_share, 6),
                "divisa": d.currency,
                "bruto": num(d.gross, 4),
                "retencion_origen": num(d.withholding_origin, 4),
                "retencion_destino": num(d.withholding_domestic, 4),
                "neto_eur": num(d.net_base),
                "origen": d.source,
            }
            for d, s in divs
        ],
    }


def tool_seguimiento(session: Session, user: User, args: dict) -> dict:
    held = portfolio.held_security_ids(session, user.id)
    out = []
    for r in screener.watchlist(session, user.id):
        v = r.valuation
        buy_ratio = r.ratio(r.buy_price)
        out.append(
            {
                "ticker": r.security.symbol,
                "nombre": r.security.name,
                "sector": r.security.sector,
                "precio": num(r.price, 4),
                "divisa": r.security.currency,
                "semaforo": SIGNALS.get(r.signal, r.signal),
                "precio_compra": num(r.buy_price, 4),
                "sobre_precio_compra_pct": pct(1 / buy_ratio - 1, 1) if buy_ratio else None,
                "precio_justo": num(v.fair_value, 4) if v else None,
                "yield_pct": pct(r.ratio(v.dividend_ttm)) if v else None,
                "yield_media_5a_pct": pct(v.yield_avg_5y) if v else None,
                "crec_div_5a_pct": pct(v.dgr_5y) if v else None,
                "rango_52s_pct": pct(r.range_52w, 0),
                "vs_media_200d_pct": pct(r.vs_ma200, 1),
                "margen_seguridad_pct": pct(r.margin_of_safety),
                "rent_total_objetivo_pct": pct(r.target_total_return),
                "en_cartera": r.security.id in held or None,
                "notas": r.item.notes,
            }
        )
    return {"valores": out}


# --- Herramientas: análisis guardados ----------------------------------------------------


def tool_guardar_analisis(session: Session, user: User, args: dict) -> dict:
    title = _str(args, "titulo", max_len=200)
    content = args.get("contenido")
    if not title:
        raise ToolError("Falta el título")
    if not isinstance(content, str) or not content.strip():
        raise ToolError("Falta el contenido")
    if len(content) > 50_000:
        raise ToolError("El contenido es demasiado largo (máximo 50 000 caracteres)")
    ticker = _str(args, "ticker", max_len=32)
    sec = _find(session, ticker) if ticker else None
    note = AiNote(
        user_id=user.id,
        security_id=sec.id if sec else None,
        title=title,
        content=content.strip(),
    )
    session.add(note)
    session.commit()
    return {"guardado": True, "id": note.id, "ticker": sec.symbol if sec else None}


def tool_historial_analisis(session: Session, user: User, args: dict) -> dict:
    limit = _int(args, "limite", 10, 1, 50)
    query = (
        select(AiNote, Security)
        .outerjoin(Security, Security.id == AiNote.security_id)
        .where(AiNote.user_id == user.id)
    )
    note_id = _num(args, "id")
    if note_id is not None:
        query = query.where(AiNote.id == int(note_id))
    ticker = _str(args, "ticker", max_len=32)
    if ticker:
        query = query.where(AiNote.security_id == _find(session, ticker).id)
    text = _str(args, "texto", max_len=100)
    if text:
        query = query.where(or_(AiNote.title.ilike(f"%{text}%"), AiNote.content.ilike(f"%{text}%")))
    rows = session.execute(query.order_by(AiNote.created_at.desc()).limit(limit)).all()
    full = _bool(args, "completo", True)
    return {
        "analisis": [
            {
                "id": n.id,
                "fecha": day(n.created_at),
                "ticker": s.symbol if s else None,
                "titulo": n.title,
                "contenido": n.content if full else n.content[:300],
            }
            for n, s in rows
        ]
    }


# --- Catálogo de herramientas -----------------------------------------------------------


@dataclass
class Tool:
    name: str
    title: str
    description: str
    schema: dict
    handler: Callable[[Session, User, dict], dict]
    read_only: bool = True


def _schema(properties: dict | None = None, required: list[str] | None = None) -> dict:
    schema = {"type": "object", "properties": properties or {}, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


TICKER = {"type": "string", "description": "Ticker de Yahoo (SAN.MC, AAPL, ENB.TO…) o ISIN"}

TOOLS = [
    Tool(
        "buscar_valor",
        "Buscar valor",
        "Busca valores del universo por ticker, nombre o ISIN y devuelve sus métricas clave "
        "(yield, precio justo, semáforo…) y si están en cartera o en seguimiento.",
        _schema(
            {"texto": {"type": "string", "description": "Parte del ticker o del nombre"}}, ["texto"]
        ),
        tool_buscar_valor,
    ),
    Tool(
        "screener",
        "Screener de dividendos",
        "Filtra el universo de acciones (EE. UU., Canadá, Europa) con los criterios del screener "
        "de la app y devuelve las filas ordenadas, con el número de resultados por sector. Los "
        "umbrales se dan en % (yield_min 4 = 4 %).",
        _schema(
            {
                "sector": {
                    "type": "string",
                    "description": "Sector de Yahoo en inglés: Utilities, Energy, Financial "
                    "Services, Real Estate, Consumer Defensive, Healthcare, Industrials…",
                },
                "regiones": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(screener.REGIONS)},
                },
                "texto": {"type": "string", "description": "Filtra por ticker o nombre"},
                "yield_min": {"type": "number"},
                "yield_max": {"type": "number"},
                "yield_media_5a_min": {"type": "number"},
                "payout_max": {"type": "number", "description": "Payout sobre BPA máximo, en %"},
                "crec_div_5a_min": {"type": "number"},
                "anios_sin_recorte_min": {"type": "integer"},
                "capitalizacion_min_mil_millones": {
                    "type": "number",
                    "description": "Capitalización mínima en miles de millones de EUR",
                },
                "solo_con_dividendo": {"type": "boolean", "default": True},
                "solo_sostenibles": {
                    "type": "boolean",
                    "description": "Solo dividendos que pasan el filtro de sostenibilidad",
                },
                "semaforo": {"type": "string", "enum": ["compra", "vigilar", "cara"]},
                "orden": {
                    "type": "string",
                    "enum": screener.SCREENER_COLUMNS,
                    "default": "yield_ttm",
                },
                "descendente": {"type": "boolean", "default": True},
                "limite": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
            }
        ),
        tool_screener,
    ),
    Tool(
        "ficha_valor",
        "Ficha de un valor",
        "Todo lo que muestra la ficha de un valor: cotización, dividendo (historial, "
        "crecimiento, payout, próximo pago), valoración (precio justo por cada método, precio "
        "de compra, semáforo, alertas de sostenibilidad), fundamentales, cuentas anuales, "
        "notas de seguimiento, la posición del usuario en él y sus análisis guardados.",
        _schema({"ticker": TICKER}, ["ticker"]),
        tool_ficha_valor,
    ),
    Tool(
        "cartera",
        "Cartera",
        "Cartera del usuario en EUR: resumen (valor, coste, plusvalía, cambio de hoy, "
        "dividendos cobrados, rentabilidad total, yield on cost), efecto de cada divisa, "
        "reparto por sector, país y divisa, y todas las posiciones abiertas con su detalle.",
        _schema({"incluir_cerradas": {"type": "boolean", "default": False}}),
        tool_cartera,
    ),
    Tool(
        "dividendos_cartera",
        "Dividendos de la cartera",
        "Proyección bruta de dividendos de los próximos 12 meses (por mes y cada pago "
        "esperado) y dividendos cobrados por año.",
        _schema(),
        tool_dividendos_cartera,
    ),
    Tool(
        "fiscal",
        "Resumen fiscal",
        "Dividendos cobrados por año de pago: bruto, retención en origen y en destino, gastos y "
        "neto en EUR, por país. Con «anio», también el detalle de cada cobro.",
        _schema({"anio": {"type": "integer"}}),
        tool_fiscal,
    ),
    Tool(
        "operaciones",
        "Operaciones",
        "Compras, ventas y dividendos cobrados registrados, del más reciente al más antiguo.",
        _schema(
            {
                "ticker": TICKER,
                "limite": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
            }
        ),
        tool_operaciones,
    ),
    Tool(
        "seguimiento",
        "Lista de seguimiento",
        "Valores que el usuario sigue, con semáforo, precio de compra (con su margen de "
        "seguridad y rentabilidad objetivo), distancia a él y sus notas.",
        _schema(),
        tool_seguimiento,
    ),
    Tool(
        "guardar_analisis",
        "Guardar análisis",
        "Guarda en la app un análisis o conclusión (Markdown) para consultarlo después, "
        "opcionalmente asociado a un valor. Úsalo cuando el usuario lo pida o quiera conservarlo.",
        _schema(
            {
                "titulo": {"type": "string", "maxLength": 200},
                "contenido": {"type": "string", "description": "Texto en Markdown"},
                "ticker": TICKER,
            },
            ["titulo", "contenido"],
        ),
        tool_guardar_analisis,
        read_only=False,
    ),
    Tool(
        "historial_analisis",
        "Historial de análisis",
        "Análisis guardados anteriormente, del más reciente al más antiguo; se pueden filtrar "
        "por valor, por texto o pedir uno por su id.",
        _schema(
            {
                "ticker": TICKER,
                "texto": {"type": "string"},
                "id": {"type": "integer"},
                "limite": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "completo": {"type": "boolean", "default": True},
            }
        ),
        tool_historial_analisis,
    ),
]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def tool_list() -> list[dict]:
    return [
        {
            "name": t.name,
            "title": t.title,
            "description": t.description,
            "inputSchema": t.schema,
            "annotations": {
                "title": t.title,
                "readOnlyHint": t.read_only,
                "destructiveHint": False,
                "idempotentHint": t.read_only,
                "openWorldHint": False,
            },
        }
        for t in TOOLS
    ]


def _short(args: dict) -> dict:
    """Argumentos para el historial: los textos largos (el contenido de un análisis) se recortan;
    el texto completo ya queda en su propia tabla."""
    return {k: v[:300] + "…" if isinstance(v, str) and len(v) > 300 else v for k, v in args.items()}


def call_tool(caller: Caller, name: str, args: dict) -> dict:
    """Ejecuta la herramienta y guarda la llamada en el historial."""
    tool = TOOLS_BY_NAME[name]
    started = time.monotonic()
    error = None
    text = ""
    try:
        with SessionLocal() as session:
            user = session.get(User, caller.user.id)
            data = _clean(tool.handler(session, user, args))
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if len(text) > MAX_RESULT_CHARS:
            raise ToolError(
                "El resultado es demasiado largo; usa un «limite» menor o filtros más concretos."
            )
    except ToolError as exc:
        error = str(exc)
    except Exception:
        log.exception("MCP: fallo en la herramienta %s", name)
        error = "Error interno al consultar los datos."
    elapsed = int((time.monotonic() - started) * 1000)
    try:
        with SessionLocal() as session:
            session.add(
                McpCall(
                    user_id=caller.user.id,
                    client=caller.client[:200],
                    tool=name,
                    arguments=_short(args),
                    ok=error is None,
                    error=error,
                    result_chars=len(text),
                    duration_ms=elapsed,
                )
            )
            session.commit()
    except Exception:
        log.exception("MCP: no se pudo guardar el historial")
    if error is not None:
        return {"content": [{"type": "text", "text": error}], "isError": True}
    return {"content": [{"type": "text", "text": text}], "isError": False}


# --- Protocolo JSON-RPC -----------------------------------------------------------------


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _result(msg_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def handle_message(caller: Caller, msg) -> dict | None:
    """Respuesta a un mensaje JSON-RPC; None si es una notificación o una respuesta."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(None, -32600, "Petición JSON-RPC no válida")
    method = msg.get("method")
    if "id" not in msg or method is None:
        return None  # notificación (p. ej. notifications/initialized) o respuesta del cliente
    msg_id = msg["id"]
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return _error(msg_id, -32602, "params debe ser un objeto")
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _result(
            msg_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": SERVER_NAME,
                    "title": "Stock Screener",
                    "version": SERVER_VERSION,
                },
                "instructions": INSTRUCTIONS,
            },
        )
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": tool_list()})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in TOOLS_BY_NAME:
            return _error(msg_id, -32602, f"Herramienta desconocida: {name}")
        if not isinstance(args, dict):
            return _error(msg_id, -32602, "arguments debe ser un objeto")
        return _result(msg_id, call_tool(caller, name, args))
    if method == "resources/list":
        return _result(msg_id, {"resources": []})
    if method == "resources/templates/list":
        return _result(msg_id, {"resourceTemplates": []})
    if method == "prompts/list":
        return _result(msg_id, {"prompts": []})
    return _error(msg_id, -32601, f"Método no admitido: {method}")


def _unauthorized(request: Request) -> JSONResponse:
    metadata = f"{public_base(request)}/.well-known/oauth-protected-resource/mcp"
    return JSONResponse(
        {"error": "invalid_token", "error_description": "Falta un token válido"},
        status_code=401,
        headers={
            "WWW-Authenticate": f'Bearer resource_metadata="{metadata}", scope="mcp"',
        },
    )


def _process(authorization: str | None, payload) -> Response | None:
    """Autentica y responde (en un hilo: las consultas a la base de datos son síncronas)."""
    with SessionLocal() as session:
        caller = caller_for(session, authorization)
    if caller is None:
        return None
    if isinstance(payload, list):
        if not payload:
            return JSONResponse(_error(None, -32600, "Lote vacío"), status_code=400)
        replies = [r for r in (handle_message(caller, m) for m in payload) if r is not None]
        return JSONResponse(replies) if replies else Response(status_code=202)
    reply = handle_message(caller, payload)
    return Response(status_code=202) if reply is None else JSONResponse(reply)


@router.post("/mcp")
@router.post("/mcp/")
async def mcp_endpoint(request: Request):
    if not get_settings().mcp_enabled:
        return JSONResponse({"error": "El conector de Claude está desactivado"}, status_code=404)
    authorization = request.headers.get("authorization")
    if not authorization:
        return _unauthorized(request)
    try:
        payload = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError):
        with SessionLocal() as session:
            if caller_for(session, authorization) is None:
                return _unauthorized(request)
        return JSONResponse(_error(None, -32700, "JSON no válido"), status_code=400)
    response = await run_in_threadpool(_process, authorization, payload)
    return _unauthorized(request) if response is None else response


@router.get("/mcp")
@router.get("/mcp/")
@router.delete("/mcp")
@router.delete("/mcp/")
def mcp_no_stream():
    # Servidor sin estado: no hay flujo SSE ni sesiones que cerrar
    return Response(status_code=405, headers={"Allow": "POST"})
