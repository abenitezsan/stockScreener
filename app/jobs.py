"""Tareas de actualización de datos. Las lanza el planificador o la línea de comandos."""

import logging
import time
from collections.abc import Iterator, Sequence
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.analysis import sectors
from app.analysis.valuation import AnnualFinancials, Inputs, Params, compute
from app.config import get_settings, today
from app.models import (
    DividendEvent,
    FinancialsAnnual,
    Fundamentals,
    FxRate,
    PriceHistory,
    Quote,
    Security,
    Valuation,
)
from app.providers import MarketDataProvider, get_provider
from app.providers.yahoo import price_divisor

log = logging.getLogger(__name__)


def _batches(items: Sequence, size: int) -> Iterator[Sequence]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _securities(
    session: Session, symbols: Sequence[str] | None = None, profiled: bool = False
) -> list[Security]:
    """Valores activos (o los indicados). `profiled`: solo los que ya tienen ficha, porque sin
    `price_currency` no se sabe si el precio viene en peniques."""
    query = select(Security).order_by(Security.symbol)
    query = query.where(Security.symbol.in_(symbols)) if symbols else query.where(Security.active)
    if profiled:
        query = query.where(Security.price_currency.is_not(None))
    return list(session.scalars(query))


class _Progress:
    """Deja en el log una línea cada `every` valores: una carga completa dura cerca de una hora."""

    def __init__(self, task: str, total: int, every: int = 50):
        self.task, self.total, self.every, self.done = task, total, every, 0
        log.info("%s: %d valores", task, total)

    def step(self) -> None:
        self.done += 1
        if self.done % self.every == 0 or self.done == self.total:
            log.info("%s: %d/%d", self.task, self.done, self.total)


def _upsert(session: Session, model, rows: list[dict], keys: list[str]) -> None:
    if not rows:
        return
    stmt = insert(model).values(rows)
    columns = {c: stmt.excluded[c] for c in rows[0] if c not in keys}
    session.execute(stmt.on_conflict_do_update(index_elements=keys, set_=columns))


def latest_fx(session: Session) -> dict[str, float]:
    """Último tipo de cambio conocido por divisa (unidades de divisa base por unidad)."""
    latest = (
        select(FxRate.currency, func.max(FxRate.day).label("day"))
        .group_by(FxRate.currency)
        .subquery()
    )
    rows = session.execute(
        select(FxRate.currency, FxRate.rate_to_base).join(
            latest, (FxRate.currency == latest.c.currency) & (FxRate.day == latest.c.day)
        )
    )
    rates = {currency: rate for currency, rate in rows}
    rates[get_settings().base_currency] = 1.0
    return rates


# --- Tareas ----------------------------------------------------------------------


def refresh_fx(session: Session, provider: MarketDataProvider | None = None) -> int:
    provider = provider or get_provider()
    currencies = session.scalars(
        select(Security.currency).where(Security.currency.is_not(None)).distinct()
    ).all()
    financial = session.scalars(
        select(Security.financial_currency)
        .where(Security.financial_currency.is_not(None))
        .distinct()
    ).all()
    wanted = set(currencies) | set(financial) | {"USD", "CAD", "GBP", "CHF", "SEK", "NOK", "DKK"}
    rates = provider.get_fx_rates(sorted(wanted), get_settings().base_currency)
    _upsert(
        session,
        FxRate,
        [{"day": today(), "currency": c, "rate_to_base": r} for c, r in rates.items()],
        ["day", "currency"],
    )
    session.commit()
    return len(rates)


def refresh_quotes(
    session: Session,
    provider: MarketDataProvider | None = None,
    symbols: Sequence[str] | None = None,
) -> int:
    provider = provider or get_provider()
    securities = {s.symbol: s for s in _securities(session, symbols, profiled=True)}
    count = 0
    for batch in _batches(list(securities), get_settings().batch_size):
        try:
            quotes = provider.get_quotes(list(batch))
        except Exception:
            log.exception("Error descargando cotizaciones")
            continue
        rows = []
        for q in quotes:
            sec = securities[q.symbol]
            divisor = price_divisor(sec.price_currency)
            rows.append(
                {
                    "security_id": sec.id,
                    "price": q.price / divisor if q.price is not None else None,
                    "previous_close": q.previous_close / divisor
                    if q.previous_close is not None
                    else None,
                    "as_of": q.as_of,
                    "updated_at": func.now(),
                }
            )
        _upsert(session, Quote, rows, ["security_id"])
        session.commit()
        count += len(rows)
    return count


def refresh_history(
    session: Session,
    provider: MarketDataProvider | None = None,
    symbols: Sequence[str] | None = None,
    period: str | None = None,
) -> int:
    """Cierres semanales y dividendos. Sin `period`: 10 años para valores sin historia y 3 meses
    para el resto."""
    provider = provider or get_provider()
    securities = _securities(session, symbols, profiled=True)
    with_history = set(session.scalars(select(PriceHistory.security_id).distinct()))
    groups: dict[str, list[Security]] = {}
    for sec in securities:
        groups.setdefault(period or ("3mo" if sec.id in with_history else "10y"), []).append(sec)

    count = 0
    progress = _Progress("histórico", len(securities), every=get_settings().batch_size)
    for per, secs in groups.items():
        by_symbol = {s.symbol: s for s in secs}
        for batch in _batches(list(by_symbol), get_settings().batch_size):
            try:
                histories = provider.get_history(list(batch), per)
            except Exception:
                log.exception("Error descargando histórico")
                continue
            for h in histories:
                sec = by_symbol[h.symbol]
                divisor = price_divisor(sec.price_currency)
                _upsert(
                    session,
                    PriceHistory,
                    [
                        {"security_id": sec.id, "day": d, "close": c / divisor}
                        for d, c in h.weekly_closes
                    ],
                    ["security_id", "day"],
                )
                _upsert(
                    session,
                    DividendEvent,
                    [
                        {"security_id": sec.id, "ex_date": d, "amount": a / divisor}
                        for d, a in h.dividends
                    ],
                    ["security_id", "ex_date"],
                )
                count += 1
            session.commit()
            for _ in batch:
                progress.step()
    return count


def refresh_profiles(
    session: Session,
    provider: MarketDataProvider | None = None,
    symbols: Sequence[str] | None = None,
) -> int:
    """Ficha y fundamentales, valor a valor (Yahoo no tiene petición en bloque para esto)."""
    provider = provider or get_provider()
    settings = get_settings()
    fx = latest_fx(session)
    count = 0
    securities = _securities(session, symbols)
    progress = _Progress("fichas", len(securities))
    for sec in securities:
        progress.step()
        try:
            profile = provider.get_profile(sec.symbol)
        except Exception:
            log.exception("Error descargando la ficha de %s", sec.symbol)
            continue
        finally:
            time.sleep(settings.request_delay)
        if not profile.name and not profile.currency:
            log.warning("%s no existe en Yahoo; se desactiva", sec.symbol)
            sec.active = False
            session.commit()
            continue
        for attr in (
            "name",
            "exchange",
            "country",
            "currency",
            "price_currency",
            "financial_currency",
            "sector",
            "industry",
        ):
            setattr(sec, attr, getattr(profile, attr))
        sec.sector_group = sectors.sector_group(profile.sector, profile.industry)
        metrics = dict(profile.metrics)
        cap, rate = metrics.get("market_cap"), fx.get(profile.currency or "")
        metrics["market_cap_eur"] = cap * rate if cap and rate else None
        _upsert(
            session,
            Fundamentals,
            [{"security_id": sec.id, **metrics, "updated_at": func.now()}],
            ["security_id"],
        )
        session.commit()
        count += 1
    return count


def refresh_financials(
    session: Session,
    provider: MarketDataProvider | None = None,
    symbols: Sequence[str] | None = None,
) -> int:
    provider = provider or get_provider()
    delay = get_settings().request_delay
    count = 0
    securities = _securities(session, symbols)
    progress = _Progress("cuentas", len(securities))
    for sec in securities:
        progress.step()
        try:
            periods = provider.get_financials(sec.symbol)
        except Exception:
            log.exception("Error descargando las cuentas de %s", sec.symbol)
            continue
        finally:
            time.sleep(delay)
        rows = [{"security_id": sec.id, **_as_dict(p)} for p in periods]
        _upsert(session, FinancialsAnnual, rows, ["security_id", "period_end"])
        session.commit()
        count += 1
    return count


def _as_dict(obj) -> dict:
    return {name: getattr(obj, name) for name in obj.__slots__}


def recompute_valuations(session: Session, symbols: Sequence[str] | None = None) -> int:
    settings = get_settings()
    params = Params(
        window_years=settings.valuation_window_years,
        margin_of_safety=settings.margin_of_safety,
        target_total_return=settings.target_total_return,
        gordon_discount_rate=settings.gordon_discount_rate,
        growth_cap=settings.growth_cap,
    )
    fx = latest_fx(session)
    now = today()
    since = now - timedelta(days=round(365.25 * 11))
    count = 0
    securities = _securities(session, symbols)
    progress = _Progress("valoración", len(securities), every=250)
    for sec in securities:
        progress.step()
        fund = session.get(Fundamentals, sec.id)
        quote = session.get(Quote, sec.id)
        payments = list(
            session.execute(
                select(DividendEvent.ex_date, DividendEvent.amount)
                .where(DividendEvent.security_id == sec.id)
                .order_by(DividendEvent.ex_date)
            )
        )
        closes = list(
            session.execute(
                select(PriceHistory.day, PriceHistory.close)
                .where(PriceHistory.security_id == sec.id, PriceHistory.day >= since)
                .order_by(PriceHistory.day)
            )
        )
        financials = [
            AnnualFinancials(
                period_end=f.period_end,
                eps=f.eps,
                free_cashflow=f.free_cashflow,
                operating_cashflow=f.operating_cashflow,
                dividends_paid=f.dividends_paid,
            )
            for f in session.scalars(
                select(FinancialsAnnual).where(FinancialsAnnual.security_id == sec.id)
            )
        ]
        fin_fx = 1.0
        if sec.financial_currency and sec.currency and sec.financial_currency != sec.currency:
            src, dst = fx.get(sec.financial_currency), fx.get(sec.currency)
            if src and dst:
                fin_fx = src / dst
            else:
                financials = []  # sin tipo de cambio no se pueden comparar con el precio
        price = quote.price if quote else (closes[-1][1] if closes else None)
        values = compute(
            Inputs(
                today=now,
                price=price,
                payments=payments,
                closes=closes,
                financials=financials,
                sector=sec.sector,
                group=sec.sector_group or sectors.GENERAL,
                eps_ttm=fund.eps_ttm if fund else None,
                eps_forward=fund.eps_forward if fund else None,
                payout_ratio=fund.payout_ratio if fund else None,
                provider_yield=fund.dividend_yield if fund else None,
                fin_to_price_fx=fin_fx,
            ),
            params,
        )
        _upsert(
            session,
            Valuation,
            [{"security_id": sec.id, **values, "computed_at": func.now()}],
            ["security_id"],
        )
        count += 1
        if count % 100 == 0:
            session.commit()
    session.commit()
    return count


# --- Agrupaciones que usa el planificador -----------------------------------------


def nightly(session: Session) -> None:
    refresh_fx(session)
    refresh_profiles(session)  # antes que el histórico: fija la divisa de cotización
    refresh_history(session)
    refresh_quotes(session)
    recompute_valuations(session)


def weekly(session: Session) -> None:
    refresh_financials(session)
    recompute_valuations(session)
