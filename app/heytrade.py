"""Lectura de los PDFs que HeyTrade envía por email: confirmaciones de operación y de abono.

Las plantillas son fijas: cada línea es «etiqueta  valor». Se extrae el texto con pypdf y se
buscan los campos por etiqueta (no por posición), así que un campo opcional que falte no rompe
la lectura. Los PDFs son datos no confiables: se validan todos los campos que se usan.

Plantillas vistas hasta ahora:
- «Confirmación de operación» (compra). La venta se supone con las mismas etiquetas y
  `Indicador de compra/venta = Venta`; no hay ejemplo real.
- «Confirmación del abono de Dividendo Nacional» (retención solo en España).
  Los dividendos extranjeros se suponen con etiquetas de retención «en origen»/«en destino»;
  tampoco hay ejemplo real.
"""

import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

MAX_PDF_BYTES = 2_000_000
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


class ParseError(ValueError):
    """El PDF no es una plantilla conocida de HeyTrade o le falta algún campo."""


@dataclass(slots=True)
class TradeDoc:
    kind: str  # buy | sell
    isin: str
    name: str
    trade_date: date
    quantity: Decimal
    price: Decimal
    currency: str
    fx_rate: Decimal  # EUR por unidad de divisa (1 si la operación es en EUR)
    fees: Decimal  # EUR: comisión de ejecución + comisión de cambio
    total_eur: Decimal  # compra: pagado con comisiones; venta: cobrado sin comisiones
    external_id: str = ""


@dataclass(slots=True)
class DividendDoc:
    isin: str
    name: str
    currency: str
    per_share: Decimal
    ex_date: date | None
    pay_date: date
    shares: Decimal
    gross: Decimal
    withholding_origin: Decimal
    withholding_domestic: Decimal
    withholding_rate: Decimal | None  # fracción (0.19)
    fees: Decimal
    net: Decimal  # neto abonado, en `currency`
    fx_rate: Decimal | None  # EUR por unidad de `currency`; None si el PDF no lo indica
    net_base: Decimal | None  # neto abonado en EUR
    external_id: str = ""


# --- Utilidades --------------------------------------------------------------------


def pdf_text(data: bytes) -> str:
    if not data.startswith(b"%PDF") or len(data) > MAX_PDF_BYTES:
        raise ParseError("No es un PDF válido (o pesa más de 2 MB)")
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(p.extract_text(extraction_mode="layout") or "" for p in reader.pages)
    except Exception as exc:
        raise ParseError("No se pudo leer el PDF") from exc


def parse_number(text: str) -> Decimal:
    """Número en formato español: 1.234,56 · 55,3950 · 19,00%."""
    cleaned = text.strip().rstrip("%").strip().replace(" ", "")
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ParseError(f"Número no válido: {text!r}") from exc
    if not value.is_finite():
        raise ParseError(f"Número no válido: {text!r}")
    return value


def _amount(text: str, default_currency: str | None = None) -> tuple[Decimal, str | None]:
    """«609,35 USD» -> (609.35, "USD")."""
    parts = text.split()
    if not parts:
        raise ParseError("Importe vacío")
    currency = (
        parts[1].upper() if len(parts) > 1 and re.fullmatch(r"[A-Za-z]{3}", parts[1]) else None
    )
    return parse_number(parts[0]), currency or default_currency


def _date(text: str) -> date:
    m = re.match(r"\s*(\d{2})/(\d{2})/(\d{4})", text)
    if not m:
        raise ParseError(f"Fecha no válida: {text!r}")
    try:
        return date(int(m[3]), int(m[2]), int(m[1]))
    except ValueError as exc:
        raise ParseError(f"Fecha no válida: {text!r}") from exc


def fields(text: str) -> dict[str, str]:
    """Pares «etiqueta -> valor» de las líneas con dos o más espacios entre ambos."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        parts = re.split(r"\s{2,}", line.strip(), maxsplit=1)
        if len(parts) == 2 and parts[0] and parts[1].strip():
            out.setdefault(parts[0].strip().lower(), parts[1].strip())
    return out


def _need(f: dict[str, str], label: str) -> str:
    try:
        return f[label.lower()]
    except KeyError:
        raise ParseError(f"Falta el campo «{label}»") from None


def _isin(f: dict[str, str]) -> str:
    isin = _need(f, "Código ISIN").upper()
    if not ISIN_RE.match(isin):
        raise ParseError(f"ISIN no válido: {isin!r}")
    return isin


def _positive(value: Decimal, label: str) -> Decimal:
    if value <= 0:
        raise ParseError(f"«{label}» debe ser positivo")
    return value


# --- Plantillas --------------------------------------------------------------------


def parse_trade(text: str) -> TradeDoc:
    f = fields(text)
    side = _need(f, "Indicador de compra/venta").lower()
    if side not in ("compra", "venta"):
        raise ParseError(f"Indicador de compra/venta desconocido: {side!r}")
    price, currency = _amount(_need(f, "Precio unitario y divisa"))
    if not currency:
        raise ParseError("El precio no indica la divisa")
    fees = Decimal(0)
    for label in ("Comisión de ejecución", "Comisión por cambio de divisa"):
        if label.lower() in f:
            fees += _amount(f[label.lower()], "EUR")[0]
    if "tipo de cambio base" in f and currency != "EUR":
        fx_rate = parse_number(f["tipo de cambio base"])
    else:
        fx_rate = Decimal(1)
    return TradeDoc(
        kind="buy" if side == "compra" else "sell",
        isin=_isin(f),
        name=f.get("nombre del valor", ""),
        trade_date=_date(_need(f, "Fecha y hora de ejecución")),
        quantity=_positive(parse_number(_need(f, "Cantidad")), "Cantidad"),
        price=_positive(price, "Precio"),
        currency=currency,
        fx_rate=_positive(fx_rate, "Tipo de cambio"),
        fees=fees,
        total_eur=_positive(_amount(_need(f, "Total en euros"), "EUR")[0], "Total en euros"),
    )


def parse_dividend(text: str) -> DividendDoc:
    f = fields(text)
    currency = _need(f, "Divisa abono").upper()
    gross, _ = _amount(_need(f, "Importe bruto"), currency)
    net, _ = _amount(_need(f, "Importe neto abonado"), currency)
    per_share = parse_number(_need(f, "Importe unitario"))
    national = "nacional" in f.get("indicador de operación", "").lower()

    origin = domestic = Decimal(0)
    rate = None
    if "tipo de retención" in f:
        rate = parse_number(f["tipo de retención"]) / 100
    for label, value in f.items():
        if not label.startswith("importe de retención") and not label.startswith(
            "importe retención"
        ):
            continue
        amount = _amount(value, currency)[0]
        if "origen" in label:
            origin += amount
        elif "destino" in label or national:
            domestic += amount
        else:
            origin += amount  # dividendo extranjero sin más detalle: retención del país de origen

    fees = Decimal(0)
    if "gastos, comisiones e impuestos" in f:
        fees = _amount(f["gastos, comisiones e impuestos"], currency)[0]
    fx_rate: Decimal | None = Decimal(1) if currency == "EUR" else None
    if currency != "EUR":
        for label in ("tipo de cambio", "tipo de cambio base"):
            if label in f:
                fx_rate = parse_number(f[label])
                break
    net_base = net * fx_rate if fx_rate else None
    if currency != "EUR" and "importe neto en euros" in f:
        net_base = _amount(f["importe neto en euros"], "EUR")[0]
        fx_rate = fx_rate or net_base / net
    if abs(gross - origin - domestic - fees - net) > Decimal("0.05"):
        raise ParseError("El bruto, las retenciones y el neto no cuadran")
    return DividendDoc(
        isin=_isin(f),
        name=f.get("nombre del valor", ""),
        currency=currency,
        per_share=per_share,
        ex_date=_date(f["fecha ex-date"]) if "fecha ex-date" in f else None,
        pay_date=_date(_need(f, "Fecha abono")),
        shares=_positive(parse_number(_need(f, "Cantidad de acciones")), "Cantidad de acciones"),
        gross=_positive(gross, "Importe bruto"),
        withholding_origin=origin,
        withholding_domestic=domestic,
        withholding_rate=rate,
        fees=fees,
        net=net,
        fx_rate=_positive(fx_rate, "Tipo de cambio") if fx_rate else None,
        net_base=net_base,
    )


def parse_pdf(data: bytes) -> TradeDoc | DividendDoc:
    """Lee un PDF de HeyTrade. `external_id` es el hash del fichero (idempotencia al reenviar)."""
    text = pdf_text(data)
    lowered = text.lower()
    if "confirmación de operación" in lowered:
        doc: TradeDoc | DividendDoc = parse_trade(text)
    elif "confirmación del abono" in lowered:
        doc = parse_dividend(text)
    else:
        raise ParseError("Plantilla desconocida: ni operación ni abono de dividendo")
    doc.external_id = hashlib.sha256(data).hexdigest()
    return doc
