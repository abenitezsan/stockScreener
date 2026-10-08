"""Rutas de la cartera: resumen, dividendos, resumen fiscal, operaciones e importación."""

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app import heytrade, mailbox, portfolio
from app.config import get_settings, today
from app.jobs import latest_fx
from app.models import (
    Contribution,
    CostAdjustment,
    DcaPlan,
    DividendPayment,
    Security,
    Transaction,
)
from app.web import ROOT, DbSession, PortfolioUser, current_user, render_security, templates

router = APIRouter(dependencies=[Depends(current_user)])

TABS = {
    "posiciones": "Posiciones",
    "dividendos": "Dividendos",
    "fiscal": "Fiscal",
    "aportaciones": "Aportaciones",
    "operaciones": "Operaciones",
    "importar": "Añadir e importar",
}
MAX_FILES = 30


def _number(text: str, label: str) -> Decimal:
    try:
        return heytrade.parse_number(text)
    except heytrade.ParseError:
        raise portfolio.PortfolioError(f"«{label}» no es un número válido") from None


def _money(text: str, label: str) -> Decimal:
    """Importe en euros: «5.000» son cinco mil (el punto solo separa miles), «5000,50» también vale."""
    cleaned = text.strip().replace(" ", "")
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+", cleaned):
        cleaned = cleaned.replace(".", "")
    return _number(cleaned, label)


def _back(value: str | None) -> str:
    """Solo se vuelve a la cartera o a una ficha (nada de redirecciones externas)."""
    if value and value.startswith(("/portfolio", "/security/")) and "//" not in value[1:]:
        return value
    return "/portfolio?tab=operaciones"


def _div_chart(cal) -> list[dict]:
    """Columnas SVG (viewBox 600x200) de la ventana: cobrado (macizo) y pendiente (claro)."""
    top = max((m.total for m in cal.months), default=0.0) or 1.0
    plot = 150.0  # alto útil
    out = []
    for i, m in enumerate(cal.months):
        got, pend = m.received_gross / top * plot, m.pending / top * plot
        out.append(
            {
                "x": 8 + i * 49,
                "got_h": got,
                "got_y": 170 - got,
                "pend_h": pend,
                "pend_y": 170 - got - pend,
                "label_y": 170 - got - pend - 4,
                "m": m,
            }
        )
    return out


def _chart(snaps) -> dict | None:
    """Puntos SVG (viewBox 600x180) de lo aportado y del valor; None si hay menos de 2 fotos."""
    if len(snaps) < 2:
        return None
    lo = min(min(float(x.invested), float(x.value)) for x in snaps)
    hi = max(max(float(x.invested), float(x.value)) for x in snaps)
    span = (hi - lo) or 1.0
    days = (snaps[-1].day - snaps[0].day).days or 1

    def points(attr: str) -> str:
        return " ".join(
            f"{(x.day - snaps[0].day).days / days * 600:.1f},"
            f"{170 - (float(getattr(x, attr)) - lo) / span * 160:.1f}"
            for x in snaps
        )

    return {
        "invested": points("invested"),
        "value": points("value"),
        "lo": lo,
        "hi": hi,
        "first": snaps[0].day,
        "last": snaps[-1].day,
    }


def _from_security(session, back: str | None) -> Security | None:
    """Valor de la ficha desde la que se envió un formulario (`back` = /security/TICKER)."""
    target = _back(back)
    if not target.startswith("/security/"):
        return None
    symbol = target.removeprefix("/security/").split("?")[0].upper()
    return session.scalar(select(Security).where(Security.symbol == symbol))


def _fail(request, session, user, back, errors, default_tab="importar"):
    """Errores de un formulario: en la ficha si venía de ella; si no, en la cartera."""
    sec = _from_security(session, back)
    if sec is not None:
        return render_security(request, sec, session, user, errors=errors, status=400)
    tab = "posiciones" if _back(back).startswith("/portfolio?tab=posiciones") else default_tab
    return _page(request, session, user, tab, errors=errors, status=400)


def _page(
    request: Request,
    session,
    user,
    tab: str,
    *,
    results: list | None = None,
    errors: list[str] | None = None,
    notice: str = "",
    status: int = 200,
    start: str = "",
):
    tab = tab if tab in TABS else "posiciones"
    rows = portfolio.positions(session, user.id, include_closed=tab == "posiciones")
    open_rows = [p for p in rows if p.is_open]
    ctx = {
        "tab": tab,
        "tabs": TABS,
        "rows": open_rows,
        "closed": [p for p in rows if not p.is_open],
        "summary": portfolio.summarize(rows),
        "results": results or [],
        "errors": errors or [],
        "notice": notice,
        "today": today(),
        "base_currency": get_settings().base_currency,
    }
    if tab in ("posiciones", "aportaciones"):
        portfolio.apply_dca(session, user.id)
        invested = float(portfolio.invested_total(session, user.id, today()))
        ctx["invested"] = invested
        ctx["has_invested"] = invested != 0 or session.get(DcaPlan, user.id) is not None
        ctx["gain_vs_invested"] = ctx["summary"].value_eur - invested
        ctx["gain_vs_invested_pct"] = ctx["gain_vs_invested"] / invested if invested > 0 else None
    if tab == "aportaciones":
        portfolio.take_snapshot(session, user.id)
        ctx["dca"] = session.get(DcaPlan, user.id)
        ctx["contributions"] = list(
            session.scalars(
                select(Contribution)
                .where(Contribution.user_id == user.id)
                .order_by(Contribution.day.desc(), Contribution.id.desc())
            )
        )
        ctx["chart"] = _chart(portfolio.snapshot_series(session, user.id))
    if tab == "posiciones":
        ctx["by_sector"] = portfolio.allocation(open_rows, lambda p: p.security.sector)
        ctx["by_country"] = portfolio.allocation(open_rows, lambda p: p.security.country)
        ctx["by_currency"] = portfolio.allocation(open_rows, lambda p: p.security.currency)
    if tab == "dividendos":
        ctx["projection"] = portfolio.project_dividends(session, open_rows)
        ctx["received"] = portfolio.tax_summary(session, user.id)
        ctx["imported_dividends"] = portfolio.imported_dividends_eur(session, user.id)
        ctx["calendar"] = cal = portfolio.dividend_calendar(
            session, user.id, ctx["projection"], start
        )
        ctx["div_chart"] = _div_chart(cal)
    if tab == "fiscal":
        ctx["years"] = portfolio.tax_summary(session, user.id)
        ctx["imported_dividends"] = portfolio.imported_dividends_eur(session, user.id)
    if tab == "importar":
        ctx["pending"] = portfolio.pending_summary(session, user.id)
        ctx["mailbox_on"] = mailbox.configured()
        ctx["mailbox"] = mailbox.STATUS
    if tab == "operaciones":
        ctx["transactions"] = session.execute(
            select(Transaction, Security)
            .join(Security, Security.id == Transaction.security_id)
            .where(Transaction.user_id == user.id)
            .order_by(Transaction.trade_date.desc(), Transaction.id.desc())
        ).all()
        ctx["adjustments"] = session.execute(
            select(CostAdjustment, Security)
            .join(Security, Security.id == CostAdjustment.security_id)
            .where(CostAdjustment.user_id == user.id)
            .order_by(CostAdjustment.day.desc(), CostAdjustment.id.desc())
        ).all()
        ctx["dividends"] = session.execute(
            select(DividendPayment, Security)
            .join(Security, Security.id == DividendPayment.security_id)
            .where(DividendPayment.user_id == user.id)
            .order_by(DividendPayment.pay_date.desc(), DividendPayment.id.desc())
        ).all()
    return templates.TemplateResponse(request, "portfolio.html", ctx, status_code=status)


@router.get("/portfolio", response_class=HTMLResponse)
def portfolio_page(
    request: Request,
    session: DbSession,
    user: PortfolioUser,
    tab: str = "posiciones",
    done: str = "",
    start: str = "",
):
    notice = {
        "op": "Operación guardada.",
        "pos": "Posición eliminada.",
        "avg": "Precio medio actualizado.",
        "contrib": "Aportación guardada.",
        "dca": "DCA guardado.",
        "deleted": "Eliminado.",
        "isin": "ISIN asignado.",
    }.get(done, "")
    return _page(request, session, user, tab, notice=notice, start=start)


@router.post("/portfolio/upload", response_class=HTMLResponse)
async def upload_pdfs(
    request: Request,
    session: DbSession,
    user: PortfolioUser,
    files: Annotated[list[UploadFile] | None, File()] = None,
):
    files = [f for f in files or [] if f.filename]
    if not files:
        return _page(
            request, session, user, "importar", errors=["Elige al menos un PDF"], status=400
        )
    results = []
    for upload in files[:MAX_FILES]:
        data = await upload.read(heytrade.MAX_PDF_BYTES + 1)
        results.append(portfolio.record_pdf(session, user.id, upload.filename or "", data))
    notice = ""
    if len(files) > MAX_FILES:
        notice = f"Solo se han leído los primeros {MAX_FILES} ficheros."
    return _page(request, session, user, "importar", results=results, notice=notice)


@router.post("/portfolio/isin", response_class=HTMLResponse)
def set_isin(
    request: Request,
    session: DbSession,
    user: PortfolioUser,
    background: BackgroundTasks,
    isin: str = Form(...),
    symbol: str = Form(...),
):
    try:
        security = portfolio.assign_isin(session, isin, symbol)
    except portfolio.PortfolioError as exc:
        return _page(request, session, user, "importar", errors=[str(exc)], status=400)
    if security.price_currency is None:  # valor nuevo: faltan ficha, histórico y cotización
        background.add_task(portfolio.fetch_market_data, security.symbol)
    # Los PDFs que esperaban este ticker (p. ej. los del buzón) se procesan ya
    results = portfolio.process_pending(session, user.id, isin.strip().upper())
    notice = f"{isin.strip().upper()} asignado a {security.symbol}."
    if results:
        notice += f" Se han procesado {len(results)} documentos pendientes."
    else:
        notice += " Si tienes PDFs de este valor, vuelve a subirlos."
    return _page(request, session, user, "importar", results=results, notice=notice)


@router.post("/portfolio/positions", response_class=HTMLResponse)
def import_positions(
    request: Request,
    session: DbSession,
    user: PortfolioUser,
    text: str = Form(""),
    as_of: str = Form(""),
):
    try:
        day = date.fromisoformat(as_of) if as_of else today()
    except ValueError:
        return _page(request, session, user, "importar", errors=["Fecha no válida"], status=400)
    rows, errors = portfolio.parse_positions(session, text[:20000])
    if errors or not rows:
        return _page(
            request,
            session,
            user,
            "importar",
            errors=errors or ["No hay ninguna posición que importar"],
            status=400,
        )
    for sec, shares, avg, dividends in rows:
        try:
            portfolio.add_transaction(
                session,
                user.id,
                sec,
                kind="buy",
                trade_date=day,
                quantity=shares,
                total_eur=(shares * avg).quantize(Decimal("0.01")),
                currency=sec.currency,
                source="import",
                notes="Posición importada",
            )
            if dividends > 0:
                portfolio.add_received_dividend(
                    session,
                    user.id,
                    sec,
                    pay_date=day,
                    shares=shares,
                    gross=dividends,
                    currency=get_settings().base_currency,
                    source="import",
                )
        except portfolio.PortfolioError as exc:
            session.rollback()
            errors.append(f"{sec.symbol}: {exc}")
    if errors:
        return _page(request, session, user, "importar", errors=errors, status=400)
    return RedirectResponse(f"{ROOT}/portfolio?tab=posiciones&done=op", status_code=303)


@router.post("/portfolio/transactions", response_class=HTMLResponse)
def add_manual_transaction(
    request: Request,
    session: DbSession,
    user: PortfolioUser,
    ident: str = Form(...),
    kind: str = Form(...),
    trade_date: str = Form(...),
    quantity: str = Form(...),
    price: str = Form(...),
    currency: str = Form(""),
    fx_rate: str = Form(""),
    fees: str = Form("0"),
    back: str = Form(""),
):
    try:
        sec = portfolio.find_security(session, ident)
        if sec is None:
            raise portfolio.PortfolioError(f"«{ident.strip()}» no está en el universo")
        try:
            day = date.fromisoformat(trade_date)
        except ValueError:
            raise portfolio.PortfolioError("Fecha no válida") from None
        qty, px = _number(quantity, "Acciones"), _number(price, "Precio")
        fee = _number(fees or "0", "Comisiones")
        cur = (currency or sec.currency or get_settings().base_currency).strip().upper()
        base = get_settings().base_currency
        if cur == base:
            fx = Decimal(1)
        elif fx_rate.strip():
            fx = _number(fx_rate, "Tipo de cambio")
        else:
            rate = latest_fx(session).get(cur)
            if rate is None:
                raise portfolio.PortfolioError(f"Indica el tipo de cambio {cur}→{base}")
            fx = Decimal(str(rate))
        if qty <= 0 or px <= 0:
            raise portfolio.PortfolioError("Acciones y precio deben ser positivos")
        total = portfolio.manual_total_eur(kind, qty, px, fx, fee).quantize(Decimal("0.01"))
        portfolio.add_transaction(
            session,
            user.id,
            sec,
            kind=kind,
            trade_date=day,
            quantity=qty,
            price=px,
            currency=cur,
            fx_rate=fx,
            fees=fee,
            total_eur=total,
        )
    except (portfolio.PortfolioError, InvalidOperation) as exc:
        session.rollback()
        return _fail(request, session, user, back, [str(exc)])
    target = _back(back)
    sep = "&" if "?" in target else "?"
    return RedirectResponse(f"{ROOT}{target}{sep}done=op", status_code=303)


@router.post("/portfolio/dividends/history", response_class=HTMLResponse)
def add_dividend_history(
    request: Request,
    session: DbSession,
    user: PortfolioUser,
    ident: str = Form(...),
    text: str = Form(...),
    currency: str = Form(""),
    fx_rate: str = Form(""),
    back: str = Form(""),
):
    try:
        sec = portfolio.find_security(session, ident)
        if sec is None:
            raise portfolio.PortfolioError(f"«{ident.strip()}» no está en el universo")
        fx = _number(fx_rate, "Tipo de cambio") if fx_rate.strip() else None
        added, skipped, errors = portfolio.add_dividend_history(
            session, user.id, sec, text[:50000], currency=currency, fx_rate=fx
        )
        if not errors and not added and not skipped:
            errors = ["No hay ningún dividendo que cargar"]
    except (portfolio.PortfolioError, InvalidOperation) as exc:
        session.rollback()
        errors, added, skipped = [str(exc)], 0, 0
    if errors:
        return _fail(request, session, user, back, errors)
    note = f"{added} dividendos cargados" + (
        f"; {skipped} ya existían y se han saltado." if skipped else "."
    )
    from_sec = _from_security(session, back)
    if from_sec is not None:
        return render_security(request, from_sec, session, user, notice=note)
    return _page(request, session, user, "dividendos", notice=note)


@router.post("/portfolio/dividends", response_class=HTMLResponse)
def add_manual_dividend(
    request: Request,
    session: DbSession,
    user: PortfolioUser,
    ident: str = Form(...),
    pay_date: str = Form(...),
    shares: str = Form(...),
    gross: str = Form(...),
    currency: str = Form(""),
    withholding_origin: str = Form("0"),
    withholding_domestic: str = Form("0"),
    fx_rate: str = Form(""),
    back: str = Form(""),
):
    try:
        sec = portfolio.find_security(session, ident)
        if sec is None:
            raise portfolio.PortfolioError(f"«{ident.strip()}» no está en el universo")
        try:
            day = date.fromisoformat(pay_date)
        except ValueError:
            raise portfolio.PortfolioError("Fecha no válida") from None
        portfolio.add_received_dividend(
            session,
            user.id,
            sec,
            pay_date=day,
            shares=_number(shares, "Acciones"),
            gross=_number(gross, "Bruto"),
            currency=currency.strip() or None,
            withholding_origin=_number(withholding_origin or "0", "Retención en origen"),
            withholding_domestic=_number(withholding_domestic or "0", "Retención en destino"),
            fx_rate=_number(fx_rate, "Tipo de cambio") if fx_rate.strip() else None,
        )
    except (portfolio.PortfolioError, InvalidOperation) as exc:
        session.rollback()
        return _fail(request, session, user, back, [str(exc)])
    target = _back(back)
    sep = "&" if "?" in target else "?"
    return RedirectResponse(f"{ROOT}{target}{sep}done=op", status_code=303)


@router.post("/portfolio/securities/{security_id}/delete")
def delete_position(
    security_id: int,
    session: DbSession,
    user: PortfolioUser,
    keep_dividends: str = Form(""),
):
    """Elimina la posición entera a mano, sin validaciones ni recálculos (ni toca lo aportado)."""
    if session.get(Security, security_id) is None:
        raise HTTPException(404)
    portfolio.delete_position(session, user.id, security_id, keep_dividends=bool(keep_dividends))
    return RedirectResponse(f"{ROOT}/portfolio?tab=posiciones&done=pos", status_code=303)


@router.post("/portfolio/securities/{security_id}/avg-cost", response_class=HTMLResponse)
def edit_average_cost(
    request: Request,
    security_id: int,
    session: DbSession,
    user: PortfolioUser,
    avg_price: str = Form(...),
    back: str = Form(""),
):
    sec = session.get(Security, security_id)
    if sec is None:
        raise HTTPException(404)
    try:
        portfolio.set_average_cost(session, user.id, sec, _number(avg_price, "Precio medio"))
    except (portfolio.PortfolioError, InvalidOperation) as exc:
        session.rollback()
        return _fail(request, session, user, back, [str(exc)], default_tab="posiciones")
    target = _back(back)
    if not target.startswith("/security/"):
        target = "/portfolio?tab=posiciones"
    return RedirectResponse(
        f"{ROOT}{target}{'&' if '?' in target else '?'}done=avg", status_code=303
    )


@router.post("/portfolio/cost-adjustments/{row_id}/delete")
def delete_cost_adjustment(row_id: int, session: DbSession, user: PortfolioUser):
    _delete(session, user, CostAdjustment, row_id)
    return RedirectResponse(f"{ROOT}/portfolio?tab=operaciones&done=deleted", status_code=303)


@router.post("/portfolio/contributions", response_class=HTMLResponse)
def add_contribution(
    request: Request,
    session: DbSession,
    user: PortfolioUser,
    kind: str = Form(...),
    day: str = Form(...),
    amount: str = Form(...),
    notes: str = Form(""),
):
    try:
        try:
            when = date.fromisoformat(day)
        except ValueError:
            raise portfolio.PortfolioError("Fecha no válida") from None
        portfolio.add_contribution(
            session,
            user.id,
            day=when,
            amount=_money(amount, "Importe"),
            kind=kind,
            notes=notes,
        )
    except (portfolio.PortfolioError, InvalidOperation) as exc:
        session.rollback()
        return _page(request, session, user, "aportaciones", errors=[str(exc)], status=400)
    return RedirectResponse(f"{ROOT}/portfolio?tab=aportaciones&done=contrib", status_code=303)


@router.post("/portfolio/contributions/{row_id}/delete")
def delete_contribution(row_id: int, session: DbSession, user: PortfolioUser):
    _delete(session, user, Contribution, row_id)
    return RedirectResponse(f"{ROOT}/portfolio?tab=aportaciones&done=deleted", status_code=303)


@router.post("/portfolio/dca", response_class=HTMLResponse)
def save_dca(
    request: Request,
    session: DbSession,
    user: PortfolioUser,
    amount: str = Form(...),
    day: int = Form(1),
    start_date: str = Form(...),
    active: str = Form(""),
):
    try:
        try:
            start = date.fromisoformat(start_date)
        except ValueError:
            raise portfolio.PortfolioError("Fecha no válida") from None
        portfolio.set_dca(
            session,
            user.id,
            amount=_money(amount, "Aportación mensual"),
            day=day,
            start_date=start,
            active=bool(active),
        )
    except (portfolio.PortfolioError, InvalidOperation) as exc:
        session.rollback()
        return _page(request, session, user, "aportaciones", errors=[str(exc)], status=400)
    return RedirectResponse(f"{ROOT}/portfolio?tab=aportaciones&done=dca", status_code=303)


@router.post("/portfolio/dca/delete")
def delete_dca(session: DbSession, user: PortfolioUser):
    plan = session.get(DcaPlan, user.id)
    if plan is not None:  # las aportaciones ya generadas se conservan
        session.delete(plan)
        session.commit()
    return RedirectResponse(f"{ROOT}/portfolio?tab=aportaciones&done=deleted", status_code=303)


def _delete(session, user, model, row_id: int):
    row = session.get(model, row_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(404)
    if model is Transaction:
        others = [
            t
            for t in session.scalars(
                select(Transaction).where(
                    Transaction.user_id == user.id, Transaction.security_id == row.security_id
                )
            )
            if t.id != row.id
        ]
        try:
            portfolio._simulate(others)
        except portfolio.PortfolioError as exc:
            return str(exc)
    session.delete(row)
    session.commit()
    return ""


@router.post("/portfolio/transactions/{tx_id}/delete", response_class=HTMLResponse)
def delete_transaction(request: Request, tx_id: int, session: DbSession, user: PortfolioUser):
    error = _delete(session, user, Transaction, tx_id)
    if error:
        return _page(request, session, user, "operaciones", errors=[error], status=400)
    return RedirectResponse(f"{ROOT}/portfolio?tab=operaciones&done=deleted", status_code=303)


@router.post("/portfolio/dividends/{pay_id}/delete")
def delete_dividend(pay_id: int, session: DbSession, user: PortfolioUser):
    _delete(session, user, DividendPayment, pay_id)
    return RedirectResponse(f"{ROOT}/portfolio?tab=operaciones&done=deleted", status_code=303)
