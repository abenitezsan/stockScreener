"""Cartera: posiciones, valor en EUR, dividendos cobrados, proyección y resumen fiscal.

Todo se calcula a partir de las operaciones (`Transaction`) y los dividendos cobrados
(`DividendPayment`) de cada usuario. Coste medio ponderado, en EUR (lo que pagaste de verdad,
con el tipo de cambio y las comisiones del bróker). Los precios de mercado se convierten con
el último tipo de cambio conocido.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app import heytrade
from app.config import get_settings, today
from app.jobs import latest_fx
from app.models import (
    Contribution,
    DcaPlan,
    DividendEvent,
    DividendPayment,
    PendingDocument,
    PortfolioSnapshot,
    Quote,
    Security,
    Transaction,
    Valuation,
)

ZERO = Decimal(0)
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-^=]{0,31}$")
MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


class PortfolioError(ValueError):
    """Dato no válido o incoherente en la cartera (se enseña tal cual al usuario)."""


class DuplicateError(PortfolioError):
    """La operación o el dividendo ya estaba guardado."""


def _d(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


# --- Alta de operaciones y dividendos ---------------------------------------------------


def find_security(session: Session, ident: str) -> Security | None:
    """Valor por ticker de Yahoo o por ISIN."""
    ident = ident.strip().upper()
    return session.scalar(
        select(Security).where(or_(Security.symbol == ident, Security.isin == ident)).limit(1)
    )


def _simulate(txs: list[Transaction]) -> None:
    shares = ZERO
    for t in sorted(txs, key=lambda t: (t.trade_date, t.id or 0)):
        shares += t.quantity if t.kind == "buy" else -t.quantity
        if shares < 0:
            raise PortfolioError(
                f"Vendes más acciones de las que tenías el {t.trade_date:%d/%m/%Y}. "
                "¿Falta importar la posición o una compra anterior?"
            )


def add_transaction(
    session: Session,
    user_id: int,
    security: Security,
    *,
    kind: str,
    trade_date: date,
    quantity: Decimal,
    total_eur: Decimal,
    price: Decimal | None = None,
    currency: str | None = None,
    fx_rate: Decimal = Decimal(1),
    fees: Decimal = ZERO,
    source: str = "manual",
    external_id: str | None = None,
    notes: str | None = None,
) -> Transaction:
    """Guarda una operación. Falla si es un duplicado o deja la posición en negativo."""
    if kind not in ("buy", "sell"):
        raise PortfolioError("La operación debe ser compra o venta")
    if quantity <= 0 or total_eur <= 0 or fees < 0 or fx_rate <= 0:
        raise PortfolioError("Cantidad, importe y tipo de cambio deben ser positivos")
    if trade_date > today() + timedelta(days=1):
        raise PortfolioError("La fecha no puede ser futura")
    if external_id and session.scalar(
        select(Transaction.id).where(
            Transaction.user_id == user_id, Transaction.external_id == external_id
        )
    ):
        raise DuplicateError("Este PDF ya estaba cargado")
    if source == "pdf" and session.scalar(
        select(Transaction.id).where(
            Transaction.user_id == user_id,
            Transaction.security_id == security.id,
            Transaction.kind == kind,
            Transaction.trade_date == trade_date,
            Transaction.quantity == quantity,
            Transaction.total_eur == total_eur,
        )
    ):
        raise DuplicateError("Ya hay una operación igual (¿añadida a mano?)")
    existing = list(
        session.scalars(
            select(Transaction).where(
                Transaction.user_id == user_id, Transaction.security_id == security.id
            )
        )
    )
    tx = Transaction(
        user_id=user_id,
        security_id=security.id,
        kind=kind,
        trade_date=trade_date,
        quantity=quantity,
        price=price,
        currency=currency or security.currency or get_settings().base_currency,
        fx_rate=fx_rate,
        fees=fees,
        total_eur=total_eur,
        source=source,
        external_id=external_id,
        notes=(notes or None),
    )
    _simulate([*existing, tx])
    session.add(tx)
    session.commit()
    return tx


def manual_total_eur(
    kind: str, quantity: Decimal, price: Decimal, fx_rate: Decimal, fees: Decimal
) -> Decimal:
    gross = quantity * price * fx_rate
    return gross + fees if kind == "buy" else gross - fees


def add_dividend(
    session: Session,
    user_id: int,
    security: Security,
    doc: heytrade.DividendDoc,
    source: str,
    commit: bool = True,
) -> DividendPayment:
    if doc.external_id and session.scalar(
        select(DividendPayment.id).where(
            DividendPayment.user_id == user_id, DividendPayment.external_id == doc.external_id
        )
    ):
        raise DuplicateError("Este PDF ya estaba cargado")
    if session.scalar(
        select(DividendPayment.id).where(
            DividendPayment.user_id == user_id,
            DividendPayment.security_id == security.id,
            DividendPayment.pay_date == doc.pay_date,
            DividendPayment.shares == doc.shares,
            DividendPayment.gross == doc.gross,
        )
    ):
        raise DuplicateError("Ya hay un dividendo igual")
    fx_rate, net_base = doc.fx_rate, doc.net_base
    if fx_rate is None:  # el PDF no trae cambio: se usa el último conocido
        rate = latest_fx(session).get(doc.currency)
        if rate is None:
            raise PortfolioError(
                f"No hay tipo de cambio {doc.currency}→EUR para valorar el dividendo"
            )
        fx_rate = Decimal(str(rate))
        net_base = (doc.net * fx_rate).quantize(Decimal("0.01"))
    pay = DividendPayment(
        user_id=user_id,
        security_id=security.id,
        ex_date=doc.ex_date,
        pay_date=doc.pay_date,
        shares=doc.shares,
        per_share=doc.per_share,
        currency=doc.currency,
        gross=doc.gross,
        withholding_origin=doc.withholding_origin,
        withholding_domestic=doc.withholding_domestic,
        withholding_rate=doc.withholding_rate,
        fees=doc.fees,
        fx_rate=fx_rate,
        net_base=net_base,
        source=source,
        external_id=doc.external_id or None,
    )
    session.add(pay)
    if commit:
        session.commit()
    return pay


def _date(text: str) -> date:
    text = text.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return date.fromisoformat(text)
        d, m, y = re.split(r"[/.\-]", text)
        return date(int(y) + 2000 if len(y) == 2 else int(y), int(m), int(d))
    except ValueError:
        raise PortfolioError(f"Fecha no válida: {text!r}") from None


def add_dividend_history(
    session: Session,
    user_id: int,
    security: Security,
    text: str,
    *,
    currency: str,
    fx_rate: Decimal | None,
) -> tuple[int, int, list[str]]:
    """Carga de golpe el histórico de dividendos cobrados de un valor.

    Una línea por pago: `fecha de pago; acciones; bruto[; ret. origen; ret. destino; gastos;
    cambio a €]` (los importes en la divisa del dividendo). Todo o nada: si una línea falla no se
    guarda ninguna. Los pagos que ya existían se saltan. Devuelve (añadidos, repetidos, errores).
    """
    base = get_settings().base_currency
    cur = (currency or security.currency or base).strip().upper()
    default_fx = ZERO
    if cur == base:
        default_fx = Decimal(1)
    elif fx_rate:
        default_fx = fx_rate
    else:
        rate = latest_fx(session).get(cur)
        if rate is None:
            return 0, 0, [f"Indica el tipo de cambio {cur}→{base}"]
        default_fx = Decimal(str(rate))
    added = skipped = 0
    errors: list[str] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cells = [c.strip() for c in re.split(r"[;\t]", line)]
        if n == 1 and cells[0].lower().startswith(("fecha", "date")):
            continue
        try:
            if not 3 <= len(cells) <= 7:
                raise PortfolioError("usa «fecha; acciones; bruto» y, opcional, retenciones/gastos")
            nums = []
            for label, cell in zip(
                ("Acciones", "Bruto", "Ret. origen", "Ret. destino", "Gastos", "Cambio"),
                cells[1:],
                strict=False,
            ):
                nums.append(heytrade.parse_number(cell) if cell else None)
            nums += [None] * (6 - len(nums))
            shares, gross, w_orig, w_dom, fees, line_fx = nums
            if shares is None or gross is None:
                raise PortfolioError("faltan acciones o importe bruto")
            add_received_dividend(
                session,
                user_id,
                security,
                pay_date=_date(cells[0]),
                shares=shares,
                gross=gross,
                currency=cur,
                fx_rate=Decimal(1) if cur == base else (line_fx or default_fx),
                withholding_origin=w_orig or ZERO,
                withholding_domestic=w_dom or ZERO,
                fees=fees or ZERO,
                commit=False,
            )
            added += 1
        except DuplicateError:
            skipped += 1
        except (PortfolioError, heytrade.ParseError) as exc:
            errors.append(f"Línea {n}: {exc}")
    if errors:
        session.rollback()
        return 0, 0, errors
    session.commit()
    return added, skipped, []


def delete_position(
    session: Session, user_id: int, security_id: int, *, keep_dividends: bool = False
) -> tuple[int, int]:
    """Borra de golpe las operaciones (y los dividendos) de un valor del usuario.

    No simula ni recalcula nada: son borrados directos. Lo aportado (`Contribution`) no se toca.
    Devuelve (operaciones, dividendos) borrados.
    """
    txs = session.execute(
        delete(Transaction).where(
            Transaction.user_id == user_id, Transaction.security_id == security_id
        )
    ).rowcount
    divs = 0
    if not keep_dividends:
        divs = session.execute(
            delete(DividendPayment).where(
                DividendPayment.user_id == user_id, DividendPayment.security_id == security_id
            )
        ).rowcount
    session.commit()
    return txs, divs


# --- Dinero aportado, DCA mensual y evolución -------------------------------------------


def add_contribution(
    session: Session, user_id: int, *, day: date, amount: Decimal, kind: str, notes: str = ""
) -> Contribution:
    if kind not in ("initial", "adjust"):
        raise PortfolioError("Tipo de aportación no válido")
    if amount == 0:
        raise PortfolioError("El importe no puede ser 0")
    if kind == "initial" and amount < 0:
        raise PortfolioError("El importe inicial debe ser positivo; para restar usa un ajuste")
    if day > today():
        raise PortfolioError("La fecha no puede ser futura")
    row = Contribution(
        user_id=user_id,
        day=day,
        amount=amount.quantize(Decimal("0.01")),
        kind=kind,
        notes=notes.strip()[:200] or None,
    )
    session.add(row)
    session.commit()
    return row


def set_dca(
    session: Session,
    user_id: int,
    *,
    amount: Decimal,
    day: int,
    start_date: date,
    active: bool = True,
) -> DcaPlan:
    if amount <= 0:
        raise PortfolioError("La aportación mensual debe ser positiva")
    if not 1 <= day <= 28:
        raise PortfolioError("El día del mes debe estar entre 1 y 28")
    plan = session.get(DcaPlan, user_id)
    if plan is None:
        plan = DcaPlan(
            user_id=user_id, amount=amount, day=day, start_date=start_date, active=active
        )
        session.add(plan)
    else:
        if start_date != plan.start_date:
            plan.applied_through = None
        elif active and not plan.active:  # reactivar: los meses en pausa no se rellenan
            plan.applied_through = today().replace(day=1) - timedelta(days=1)
        plan.amount, plan.day, plan.start_date, plan.active = amount, day, start_date, active
    session.commit()
    return plan


def apply_dca(session: Session, user_id: int, upto: date | None = None) -> int:
    """Genera las aportaciones mensuales pendientes del plan DCA hasta `upto` (por defecto hoy).

    Cada mes se genera una sola vez (`applied_through`); lo que luego borres o ajustes a mano
    no se vuelve a crear.
    """
    plan = session.get(DcaPlan, user_id)
    upto = upto or today()
    if plan is None or not plan.active:
        return 0
    year, month = plan.start_date.year, plan.start_date.month
    if plan.applied_through:
        year, month = plan.applied_through.year, plan.applied_through.month + 1
        if month == 13:
            year, month = year + 1, 1
    created = 0
    last = plan.applied_through
    while (year, month) <= (upto.year, upto.month):
        due = date(year, month, plan.day)
        if due > upto:
            break
        key = f"dca:{year}-{month:02d}"
        if due >= plan.start_date and not session.scalar(
            select(Contribution.id).where(
                Contribution.user_id == user_id, Contribution.external_id == key
            )
        ):
            session.add(
                Contribution(
                    user_id=user_id,
                    day=due,
                    amount=plan.amount,
                    kind="dca",
                    external_id=key,
                    notes="DCA mensual",
                )
            )
            created += 1
        last = due
        month += 1
        if month == 13:
            year, month = year + 1, 1
    plan.applied_through = last
    session.commit()
    return created


def invested_total(session: Session, user_id: int, upto: date | None = None) -> Decimal:
    """Dinero aportado: solo suman las aportaciones; compras, ventas y dividendos nunca lo tocan.

    Las compras posteriores se entienden financiadas con dividendos o ventas, no con dinero nuevo.
    """
    query = select(func.coalesce(func.sum(Contribution.amount), 0)).where(
        Contribution.user_id == user_id
    )
    if upto:
        query = query.where(Contribution.day <= upto)
    return _d(session.scalar(query) or 0)


def take_snapshot(session: Session, user_id: int) -> PortfolioSnapshot | None:
    """Guarda (o actualiza) la foto de hoy: lo aportado frente al valor de la cartera."""
    rows = positions(session, user_id)
    invested = invested_total(session, user_id, today())
    if not rows and invested == 0:
        return None
    value = Decimal(str(round(summarize(rows).value_eur or 0, 2)))
    snap = session.get(PortfolioSnapshot, (user_id, today()))
    if snap is None:
        snap = PortfolioSnapshot(user_id=user_id, day=today(), invested=invested, value=value)
        session.add(snap)
    else:
        snap.invested, snap.value = invested, value
    session.commit()
    return snap


def snapshot_all(session: Session) -> int:
    """Tarea nocturna: aplica el DCA y guarda la foto de cada usuario con cartera."""
    user_ids = set(session.scalars(select(Transaction.user_id).distinct()))
    user_ids |= set(session.scalars(select(Contribution.user_id).distinct()))
    user_ids |= set(session.scalars(select(DcaPlan.user_id)))
    for uid in sorted(user_ids):
        apply_dca(session, uid)
        take_snapshot(session, uid)
    return len(user_ids)


def snapshot_series(session: Session, user_id: int) -> list[PortfolioSnapshot]:
    return list(
        session.scalars(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.user_id == user_id)
            .order_by(PortfolioSnapshot.day)
        )
    )


@dataclass
class DocResult:
    filename: str
    status: str  # ok | duplicate | unknown_isin | error
    message: str
    isin: str = ""
    name: str = ""


def record_pdf(session: Session, user_id: int, filename: str, data: bytes) -> DocResult:
    """Lee un PDF de HeyTrade y lo guarda. Nunca lanza: devuelve el resultado para mostrarlo."""
    try:
        doc = heytrade.parse_pdf(data)
    except heytrade.ParseError as exc:
        return DocResult(filename, "error", str(exc))
    security = find_security(session, doc.isin)
    if security is None:
        return DocResult(
            filename,
            "unknown_isin",
            f"{doc.name or doc.isin}: no sé qué ticker de Yahoo le corresponde",
            doc.isin,
            doc.name,
        )
    try:
        if isinstance(doc, heytrade.TradeDoc):
            add_transaction(
                session,
                user_id,
                security,
                kind=doc.kind,
                trade_date=doc.trade_date,
                quantity=doc.quantity,
                total_eur=doc.total_eur,
                price=doc.price,
                currency=doc.currency,
                fx_rate=doc.fx_rate,
                fees=doc.fees,
                source="pdf",
                external_id=doc.external_id,
            )
            what = "Compra" if doc.kind == "buy" else "Venta"
            return DocResult(
                filename, "ok", f"{what} de {doc.quantity:f} {security.symbol} guardada", doc.isin
            )
        add_dividend(session, user_id, security, doc, "pdf")
        return DocResult(
            filename,
            "ok",
            f"Dividendo de {security.symbol} ({doc.gross:f} {doc.currency}) guardado",
            doc.isin,
        )
    except PortfolioError as exc:
        session.rollback()
        status = "duplicate" if isinstance(exc, DuplicateError) else "error"
        return DocResult(filename, status, f"{security.symbol}: {exc}", doc.isin)


def add_received_dividend(
    session: Session,
    user_id: int,
    security: Security,
    *,
    pay_date: date,
    shares: Decimal,
    gross: Decimal,
    currency: str | None = None,
    withholding_origin: Decimal = ZERO,
    withholding_domestic: Decimal = ZERO,
    fx_rate: Decimal | None = None,
    ex_date: date | None = None,
    fees: Decimal = ZERO,
    source: str = "manual",
    commit: bool = True,
) -> DividendPayment:
    """Dividendo ya cobrado, a mano o importado junto a una posición (bruto y retenciones)."""
    base = get_settings().base_currency
    cur = (currency or security.currency or base).upper()
    if gross <= 0 or shares <= 0 or min(withholding_origin, withholding_domestic, fees) < 0:
        raise PortfolioError("Acciones y bruto deben ser positivos y las retenciones no negativas")
    if ex_date and ex_date > pay_date:
        raise PortfolioError("La fecha ex-dividendo es posterior al pago")
    if withholding_origin + withholding_domestic + fees > gross:
        raise PortfolioError("Las retenciones no pueden superar el bruto")
    if pay_date > today():
        raise PortfolioError("La fecha de cobro no puede ser futura")
    if cur == base:
        fx_rate = Decimal(1)
    elif fx_rate is None:
        rate = latest_fx(session).get(cur)
        if rate is None:
            raise PortfolioError(f"Indica el tipo de cambio {cur}→{base}")
        fx_rate = Decimal(str(rate))
    net = gross - withholding_origin - withholding_domestic - fees
    doc = heytrade.DividendDoc(
        isin=security.isin or "",
        name=security.name or "",
        currency=cur,
        per_share=(gross / shares).quantize(Decimal("0.000001")),
        ex_date=ex_date,
        pay_date=pay_date,
        shares=shares,
        gross=gross,
        withholding_origin=withholding_origin,
        withholding_domestic=withholding_domestic,
        withholding_rate=None,
        fees=fees,
        net=net,
        fx_rate=fx_rate,
        net_base=(net * fx_rate).quantize(Decimal("0.01")),
    )
    return add_dividend(session, user_id, security, doc, source, commit)


def store_pending(session: Session, user_id: int, result: DocResult, data: bytes) -> None:
    """Guarda un PDF cuyo ISIN no se reconoce hasta que se asigne su ticker."""
    import hashlib

    digest = hashlib.sha256(data).hexdigest()
    if session.scalar(
        select(PendingDocument.id).where(
            PendingDocument.user_id == user_id, PendingDocument.sha256 == digest
        )
    ):
        return
    session.add(
        PendingDocument(
            user_id=user_id,
            isin=result.isin,
            name=result.name or None,
            filename=(result.filename or "adjunto.pdf")[:255],
            sha256=digest,
            data=data,
        )
    )
    session.commit()


def pending_summary(session: Session, user_id: int) -> list[tuple[str, str, int]]:
    """(ISIN, nombre, nº de documentos) de lo que espera un ticker."""
    rows = session.execute(
        select(PendingDocument.isin, func.max(PendingDocument.name), func.count())
        .where(PendingDocument.user_id == user_id)
        .group_by(PendingDocument.isin)
        .order_by(PendingDocument.isin)
    ).all()
    return [(isin, name or "", n) for isin, name, n in rows]


def process_pending(session: Session, user_id: int, isin: str | None = None) -> list[DocResult]:
    """Procesa los PDFs pendientes (de un ISIN, o todos) cuyo valor ya se conoce."""
    query = select(PendingDocument).where(PendingDocument.user_id == user_id)
    if isin:
        query = query.where(PendingDocument.isin == isin)
    results = []
    for doc in list(session.scalars(query)):
        if find_security(session, doc.isin) is None:
            continue
        result = record_pdf(session, user_id, doc.filename, doc.data)
        if result.status in ("ok", "duplicate"):
            session.delete(doc)
            session.commit()
        results.append(result)
    return results


def assign_isin(session: Session, isin: str, symbol: str) -> Security:
    """Enlaza un ISIN con un ticker de Yahoo; si el valor no existe, lo da de alta."""
    from app import universe

    isin, symbol = isin.strip().upper(), symbol.strip().upper()
    if not heytrade.ISIN_RE.match(isin) or not SYMBOL_RE.match(symbol):
        raise PortfolioError("ISIN o ticker no válidos")
    owner = session.scalar(select(Security).where(Security.isin == isin))
    if owner and owner.symbol != symbol:
        raise PortfolioError(f"El ISIN ya está asignado a {owner.symbol}")
    security = session.scalar(select(Security).where(Security.symbol == symbol))
    if security is None:
        universe.upsert_universe(session, universe.MANUAL, [symbol])
        security = session.scalar(select(Security).where(Security.symbol == symbol))
    elif not security.active or universe.MANUAL not in (security.universes or []):
        security.universes = [*(security.universes or []), universe.MANUAL]
        security.active = True
    security.isin = isin
    session.commit()
    return security


def fetch_market_data(symbol: str) -> None:
    """Descarga ficha, histórico y cotización de un valor recién dado de alta (tarea de fondo)."""
    import logging

    from app import jobs
    from app.db import SessionLocal

    try:
        with SessionLocal() as session:
            jobs.refresh_fx(session)
            jobs.refresh_profiles(session, symbols=[symbol])
            jobs.refresh_history(session, symbols=[symbol])
            jobs.refresh_quotes(session, symbols=[symbol])
            jobs.refresh_financials(session, symbols=[symbol])
            jobs.recompute_valuations(session, symbols=[symbol])
    except Exception:
        logging.getLogger(__name__).exception("No se pudieron descargar datos de %s", symbol)


def parse_positions(session: Session, text: str) -> tuple[list[tuple], list[str]]:
    """Posiciones pegadas, una por línea: `ticker o ISIN; acciones; coste medio en EUR` y,
    opcionalmente, `; dividendos ya cobrados (bruto, EUR, acumulado)`.

    Devuelve las filas válidas (valor, acciones, coste medio, dividendos) y los errores por línea.
    """
    rows, errors = [], []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [p.strip() for p in re.split(r"[;\t]|(?<=\S),(?=\s)|\s{2,}", line.strip()) if p]
        if len(parts) == 1:
            parts = line.split()
        if len(parts) not in (3, 4):
            errors.append(
                f"Línea {n}: se esperan 3 o 4 columnas (ticker, acciones, coste medio, dividendos)"
            )
            continue
        sec = find_security(session, parts[0])
        if sec is None:
            errors.append(
                f"Línea {n}: «{parts[0]}» no está en el universo (ticker de Yahoo o ISIN)"
            )
            continue
        try:
            shares, avg = heytrade.parse_number(parts[1]), heytrade.parse_number(parts[2])
            dividends = heytrade.parse_number(parts[3]) if len(parts) == 4 else ZERO
        except heytrade.ParseError:
            errors.append(f"Línea {n}: número no válido")
            continue
        if shares <= 0 or avg <= 0 or dividends < 0:
            errors.append(f"Línea {n}: acciones y coste medio deben ser positivos")
            continue
        rows.append((sec, shares, avg, dividends))
    return rows, errors


# --- Posiciones ---------------------------------------------------------------------------


@dataclass
class LastDividend:
    ex_date: date
    amount: float  # por acción, en la divisa del valor
    change: float | None  # variación frente al anterior


@dataclass
class Position:
    security: Security
    shares: Decimal = ZERO
    cost_eur: Decimal = ZERO  # coste de las acciones que siguen en cartera
    invested_eur: Decimal = ZERO  # total comprado
    sold_eur: Decimal = ZERO  # total cobrado en ventas (neto de comisiones)
    realized_eur: Decimal = ZERO
    dividends_gross_eur: Decimal = ZERO
    dividends_net_eur: Decimal = ZERO
    price: float | None = None  # divisa del valor
    previous_close: float | None = None
    fx: float | None = None
    annual_dividend_ps: float | None = None  # por acción, divisa del valor
    last_dividend: LastDividend | None = None

    @property
    def is_open(self) -> bool:
        return self.shares > 0

    @property
    def avg_cost_eur(self) -> float | None:
        return float(self.cost_eur / self.shares) if self.shares > 0 else None

    @property
    def value_eur(self) -> float | None:
        if self.price is None or self.fx is None:
            return None
        return float(self.shares) * self.price * self.fx

    @property
    def day_change_eur(self) -> float | None:
        if self.price is None or self.previous_close is None or self.fx is None:
            return None
        return float(self.shares) * (self.price - self.previous_close) * self.fx

    @property
    def unrealized_eur(self) -> float | None:
        value = self.value_eur
        return None if value is None else value - float(self.cost_eur)

    @property
    def unrealized_pct(self) -> float | None:
        u = self.unrealized_eur
        return u / float(self.cost_eur) if u is not None and self.cost_eur > 0 else None

    @property
    def total_return_eur(self) -> float | None:
        u = self.unrealized_eur
        if u is None:
            return None
        return u + float(self.realized_eur + self.dividends_gross_eur)

    @property
    def total_return_pct(self) -> float | None:
        t = self.total_return_eur
        return t / float(self.invested_eur) if t is not None and self.invested_eur > 0 else None

    @property
    def annual_dividend_eur(self) -> float | None:
        if self.annual_dividend_ps is None or self.fx is None or not self.is_open:
            return None
        return float(self.shares) * self.annual_dividend_ps * self.fx

    @property
    def current_yield(self) -> float | None:
        if self.annual_dividend_ps is None or not self.price:
            return None
        return self.annual_dividend_ps / self.price

    @property
    def yoc(self) -> float | None:
        """Dividendo anual (a cambio actual) sobre lo que costó la posición."""
        annual = self.annual_dividend_eur
        return annual / float(self.cost_eur) if annual is not None and self.cost_eur > 0 else None


@dataclass
class Summary:
    value_eur: float = 0.0
    cost_eur: float = 0.0
    day_change_eur: float = 0.0
    unrealized_eur: float = 0.0
    realized_eur: float = 0.0
    dividends_gross_eur: float = 0.0
    dividends_net_eur: float = 0.0
    total_return_eur: float = 0.0
    invested_eur: float = 0.0
    annual_dividend_eur: float = 0.0
    missing_prices: list[str] = field(default_factory=list)

    @property
    def unrealized_pct(self) -> float | None:
        return self.unrealized_eur / self.cost_eur if self.cost_eur else None

    @property
    def total_return_pct(self) -> float | None:
        return self.total_return_eur / self.invested_eur if self.invested_eur else None

    @property
    def yield_now(self) -> float | None:
        return self.annual_dividend_eur / self.value_eur if self.value_eur else None

    @property
    def yoc(self) -> float | None:
        return self.annual_dividend_eur / self.cost_eur if self.cost_eur else None


def _last_dividend(session: Session, security_id: int) -> LastDividend | None:
    rows = session.execute(
        select(DividendEvent.ex_date, DividendEvent.amount)
        .where(DividendEvent.security_id == security_id)
        .order_by(DividendEvent.ex_date.desc())
        .limit(2)
    ).all()
    if not rows:
        return None
    change = rows[0][1] / rows[1][1] - 1 if len(rows) > 1 and rows[1][1] else None
    return LastDividend(rows[0][0], rows[0][1], change)


def positions(session: Session, user_id: int, include_closed: bool = False) -> list[Position]:
    fx = latest_fx(session)
    by_sec: dict[int, Position] = {}
    txs = session.scalars(
        select(Transaction)
        .where(Transaction.user_id == user_id)
        .order_by(Transaction.trade_date, Transaction.id)
    )
    for t in txs:
        pos = by_sec.get(t.security_id)
        if pos is None:
            pos = by_sec[t.security_id] = Position(session.get(Security, t.security_id))
        if t.kind == "buy":
            pos.shares += t.quantity
            pos.cost_eur += t.total_eur
            pos.invested_eur += t.total_eur
        else:
            avg = pos.cost_eur / pos.shares if pos.shares else ZERO
            removed = avg * t.quantity
            pos.shares -= t.quantity
            pos.cost_eur -= removed
            pos.sold_eur += t.total_eur
            pos.realized_eur += t.total_eur - removed
    for pay in session.scalars(select(DividendPayment).where(DividendPayment.user_id == user_id)):
        pos = by_sec.get(pay.security_id)
        if pos is None:  # dividendo de un valor sin operaciones: se muestra igualmente
            pos = by_sec[pay.security_id] = Position(session.get(Security, pay.security_id))
        pos.dividends_gross_eur += pay.gross * pay.fx_rate
        pos.dividends_net_eur += pay.net_base
    out = []
    for pos in by_sec.values():
        if not pos.is_open and not include_closed:
            continue
        sec = pos.security
        quote = session.get(Quote, sec.id)
        val = session.get(Valuation, sec.id)
        pos.price = quote.price if quote else None
        pos.previous_close = quote.previous_close if quote else None
        pos.fx = fx.get(sec.currency) if sec.currency else None
        if val:
            pos.annual_dividend_ps = val.dividend_forward or val.dividend_ttm
        pos.last_dividend = _last_dividend(session, sec.id)
        out.append(pos)
    return sorted(out, key=lambda p: -(p.value_eur or 0))


def summarize(rows: list[Position]) -> Summary:
    s = Summary()
    for p in rows:
        s.realized_eur += float(p.realized_eur)
        s.dividends_gross_eur += float(p.dividends_gross_eur)
        s.dividends_net_eur += float(p.dividends_net_eur)
        s.invested_eur += float(p.invested_eur)
        if not p.is_open:
            s.total_return_eur += float(p.realized_eur + p.dividends_gross_eur)
            continue
        if p.value_eur is None:
            s.missing_prices.append(p.security.symbol)
            continue
        s.value_eur += p.value_eur
        s.cost_eur += float(p.cost_eur)
        s.day_change_eur += p.day_change_eur or 0.0
        s.unrealized_eur += p.unrealized_eur or 0.0
        s.annual_dividend_eur += p.annual_dividend_eur or 0.0
        s.total_return_eur += p.total_return_eur or 0.0
    return s


def allocation(rows: list[Position], key) -> list[tuple[str, float]]:
    """Valor en EUR por grupo (`key(posición)` -> etiqueta), de mayor a menor."""
    totals: dict[str, float] = defaultdict(float)
    for p in rows:
        if p.value_eur:
            totals[key(p) or "Sin clasificar"] += p.value_eur
    return sorted(totals.items(), key=lambda kv: -kv[1])


# --- Proyección de dividendos --------------------------------------------------------------


@dataclass
class Projection:
    months: list[tuple[int, int, float]]  # (año, mes, EUR bruto)
    total_eur: float
    estimated: list[str]  # valores repartidos por igual por falta de calendario


def project_dividends(session: Session, rows: list[Position]) -> Projection:
    """Dividendos brutos de los próximos 12 meses.

    Para cada valor se repite el calendario de los últimos 12 meses (mes de la fecha ex), con el
    dividendo actual: acciones × importe de cada pago × (dividendo estimado ÷ dividendo 12 m).
    Sin calendario, el dividendo anual se reparte por igual en los 12 meses.
    """
    now = today()
    window = now - timedelta(days=365)
    buckets = [0.0] * 12  # índice = meses desde el actual
    estimated: list[str] = []
    for p in rows:
        if not p.is_open or p.fx is None or p.annual_dividend_ps is None:
            continue
        val = session.get(Valuation, p.security.id)
        events = session.execute(
            select(DividendEvent.ex_date, DividendEvent.amount).where(
                DividendEvent.security_id == p.security.id, DividendEvent.ex_date > window
            )
        ).all()
        ttm = sum(a for _, a in events)
        if events and ttm > 0:
            scale = p.annual_dividend_ps / ttm
            if val and val.dividend_ttm and val.dividend_forward:
                scale = val.dividend_forward / val.dividend_ttm
            for ex_date, amount in events:
                offset = (ex_date.year * 12 + ex_date.month) - (now.year * 12 + now.month)
                offset %= 12
                buckets[offset] += float(p.shares) * amount * scale * p.fx
        else:
            estimated.append(p.security.symbol)
            for i in range(12):
                buckets[i] += (p.annual_dividend_eur or 0.0) / 12
    months = []
    for i, amount in enumerate(buckets):
        index = now.year * 12 + now.month - 1 + i
        months.append((index // 12, index % 12 + 1, amount))
    return Projection(months, sum(buckets), estimated)


@dataclass
class CalendarMonth:
    year: int
    month: int
    received_gross: float = 0.0
    received_net: float = 0.0
    pending: float = 0.0  # esperado y aún sin cobrar (mes actual y siguientes), bruto
    future: bool = False  # el mes actual o posterior

    @property
    def total(self) -> float:
        return self.received_gross + self.pending


@dataclass
class DividendCalendar:
    months: list[CalendarMonth]
    start: str  # «AAAA-MM» del primer mes de la ventana
    prev_start: str | None  # ventana anterior (12 meses atrás), None si no hay datos más antiguos
    next_start: str | None  # ventana siguiente, None si ya se ve la de los próximos 12 meses
    is_current: bool  # la ventana empieza en el mes actual
    avg_received_gross: float  # media mensual cobrada en los últimos meses completos
    avg_received_net: float
    avg_months: int  # meses sobre los que se calcula esa media
    window_received_gross: float
    window_expected_gross: float  # cobrado + pendiente en la ventana


def _ym(index: int) -> str:
    return f"{index // 12}-{index % 12 + 1:02d}"


def dividend_calendar(
    session: Session, user_id: int, projection: Projection, start: str = ""
) -> DividendCalendar:
    """Dividendos por mes de una ventana de 12 meses: cobrados (por fecha de pago) y esperados.

    Por defecto la ventana son los próximos 12 meses (desde el actual); `start` («AAAA-MM») la
    mueve a periodos anteriores. Los meses pasados solo llevan lo cobrado; el actual y los
    siguientes, lo cobrado más lo esperado que falta (proyección, ver `project_dividends`).
    """
    now = today()
    now_idx = now.year * 12 + now.month - 1
    received: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for pay in session.scalars(
        select(DividendPayment).where(
            DividendPayment.user_id == user_id,
            DividendPayment.source != "import",  # sin fecha real de cobro: no entran por mes
        )
    ):
        bucket = received[pay.pay_date.year * 12 + pay.pay_date.month - 1]
        bucket[0] += float(pay.gross * pay.fx_rate)
        bucket[1] += float(pay.net_base)
    first_idx = min(received, default=now_idx)
    try:
        year, month = (int(x) for x in start.split("-"))
        start_idx = year * 12 + month - 1
    except ValueError:
        start_idx = now_idx
    start_idx = max(min(start_idx, now_idx), first_idx - 11)
    expected = {now_idx + i: amount for i, (_, _, amount) in enumerate(projection.months)}
    months = []
    for idx in range(start_idx, start_idx + 12):
        gross, net = received.get(idx, (0.0, 0.0))
        future = idx >= now_idx
        pending = max(expected.get(idx, 0.0) - gross, 0.0) if future else 0.0
        months.append(CalendarMonth(idx // 12, idx % 12 + 1, gross, net, pending, future))
    n = min(12, max(now_idx - first_idx, 0))  # meses completos con historia, máximo 12
    done = [received.get(i, (0.0, 0.0)) for i in range(now_idx - n, now_idx)]
    return DividendCalendar(
        months=months,
        start=_ym(start_idx),
        prev_start=_ym(start_idx - 12) if start_idx - 12 >= first_idx - 11 else None,
        next_start=_ym(min(start_idx + 12, now_idx)) if start_idx < now_idx else None,
        is_current=start_idx == now_idx,
        avg_received_gross=sum(g for g, _ in done) / n if n else 0.0,
        avg_received_net=sum(x for _, x in done) / n if n else 0.0,
        avg_months=n,
        window_received_gross=sum(m.received_gross for m in months),
        window_expected_gross=sum(m.total for m in months),
    )


# --- Resumen fiscal ------------------------------------------------------------------------


@dataclass
class TaxRow:
    payment: DividendPayment
    security: Security
    gross_eur: float
    origin_eur: float
    domestic_eur: float
    fees_eur: float
    net_eur: float


@dataclass
class TaxYear:
    year: int
    rows: list[TaxRow]
    gross_eur: float = 0.0
    origin_eur: float = 0.0
    domestic_eur: float = 0.0
    fees_eur: float = 0.0
    net_eur: float = 0.0
    by_country: dict[str, dict[str, float]] = field(default_factory=dict)


def tax_summary(session: Session, user_id: int) -> list[TaxYear]:
    """Dividendos cobrados por año (de pago) con bruto y retenciones en origen y destino, en EUR."""
    years: dict[int, TaxYear] = {}
    payments = session.execute(
        select(DividendPayment, Security)
        .join(Security, Security.id == DividendPayment.security_id)
        .where(DividendPayment.user_id == user_id, DividendPayment.source != "import")
        .order_by(DividendPayment.pay_date)
    ).all()
    for pay, sec in payments:
        fx = float(pay.fx_rate)
        row = TaxRow(
            pay,
            sec,
            float(pay.gross) * fx,
            float(pay.withholding_origin) * fx,
            float(pay.withholding_domestic) * fx,
            float(pay.fees) * fx,
            float(pay.net_base),
        )
        y = years.setdefault(pay.pay_date.year, TaxYear(pay.pay_date.year, []))
        y.rows.append(row)
        y.gross_eur += row.gross_eur
        y.origin_eur += row.origin_eur
        y.domestic_eur += row.domestic_eur
        y.fees_eur += row.fees_eur
        y.net_eur += row.net_eur
        c = y.by_country.setdefault(
            sec.country or "Desconocido", {"gross": 0.0, "origin": 0.0, "domestic": 0.0}
        )
        c["gross"] += row.gross_eur
        c["origin"] += row.origin_eur
        c["domestic"] += row.domestic_eur
    return sorted(years.values(), key=lambda y: -y.year)


def imported_dividends_eur(session: Session, user_id: int) -> float:
    """Dividendos cobrados antes de empezar a usar la app (importados con la posición), en EUR.

    Cuentan en el total return, pero no entran en el resumen fiscal: no se sabe en qué año se
    cobraron ni sus retenciones.
    """
    total = session.scalar(
        select(func.sum(DividendPayment.net_base)).where(
            DividendPayment.user_id == user_id, DividendPayment.source == "import"
        )
    )
    return float(total or 0)


def held_security_ids(session: Session, user_id: int | None) -> set[int]:
    """Valores con posición abierta del usuario (para resaltarlos en el screener y el seguimiento)."""
    if user_id is None:
        return set()
    shares: dict[int, Decimal] = defaultdict(lambda: ZERO)
    for security_id, kind, quantity in session.execute(
        select(Transaction.security_id, Transaction.kind, Transaction.quantity).where(
            Transaction.user_id == user_id
        )
    ):
        shares[security_id] += quantity if kind == "buy" else -quantity
    return {sid for sid, qty in shares.items() if qty > 0}


def position_for(session: Session, user_id: int, security_id: int) -> Position | None:
    """Posición del usuario en un valor (para el bloque «Mi posición» de su ficha)."""
    has = session.scalar(
        select(func.count())
        .select_from(Transaction)
        .where(Transaction.user_id == user_id, Transaction.security_id == security_id)
    ) or session.scalar(
        select(func.count())
        .select_from(DividendPayment)
        .where(DividendPayment.user_id == user_id, DividendPayment.security_id == security_id)
    )
    if not has:
        return None
    return next(
        (
            p
            for p in positions(session, user_id, include_closed=True)
            if p.security.id == security_id
        ),
        None,
    )
