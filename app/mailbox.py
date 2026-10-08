"""Lectura automática del buzón IMAP donde llegan reenviados los PDFs de HeyTrade.

Flujo: cada pocos minutos se leen los correos sin leer, se comprueba que el remitente está
permitido y que el proveedor del buzón certifica su autenticidad, se extraen los PDFs adjuntos
y se guardan con `portfolio.record_pdf` (idempotente: reprocesar un PDF no duplica nada).
Los correos tratados se marcan como leídos; si algo falla de forma transitoria se dejan sin leer
para reintentarlo en la siguiente vuelta.

Seguridad: los PDFs no van firmados, así que quien pudiera escribir al buzón podría colar uno
falso. Se acepta un correo solo si (1) su remitente figura en `IMAP_ALLOWED_SENDERS` y (2) la
cabecera `Authentication-Results` que añade el proveedor del buzón (`IMAP_AUTHSERV`, la más
reciente, la de arriba) da `dmarc=pass` para el dominio del remitente o `dkim=pass` alineado con él.
"""

import imaplib
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Protocol, Self

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import heytrade, portfolio
from app.config import get_settings, utcnow
from app.models import User

log = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 10_000_000
MAX_PDFS_PER_MESSAGE = 10
MAX_MESSAGES_PER_RUN = 50


@dataclass
class MailStatus:
    """Estado de la última revisión (en memoria: se reinicia con la app)."""

    last_run: datetime | None = None
    last_error: str = ""
    messages: int = 0
    documents: int = 0
    log: list[str] = field(default_factory=list)  # últimas incidencias


STATUS = MailStatus()


def configured() -> bool:
    s = get_settings()
    return bool(s.imap_host and s.imap_user and s.imap_password and allowed_senders())


def allowed_senders() -> set[str]:
    return {a.strip().lower() for a in get_settings().imap_allowed_senders.split(",") if a.strip()}


# --- Verificación del remitente -----------------------------------------------------------


def _auth_results(msg: EmailMessage, authserv: str) -> dict[str, list[tuple[str, dict]]]:
    """Resultados de autenticación de la cabecera más reciente de `authserv`."""
    for header in msg.get_all("Authentication-Results") or []:
        text = re.sub(r"\([^)]*\)", "", str(header).replace("\r", " ").replace("\n", " "))
        clauses = [c.strip() for c in text.split(";")]
        if not clauses or not clauses[0].split():
            continue
        if clauses[0].split()[0].lower() != authserv.lower():
            continue  # cabecera de otro servidor (o inyectada en el origen): se ignora
        results: dict[str, list[tuple[str, dict]]] = {}
        for clause in clauses[1:]:
            m = re.match(r"([\w-]+)=([\w-]+)", clause)
            if not m:
                continue
            props = {k.lower(): v for k, v in re.findall(r"([\w.-]+)=([^\s;]+)", clause[m.end() :])}
            results.setdefault(m[1].lower(), []).append((m[2].lower(), props))
        return results
    return {}


def check_sender(msg: EmailMessage) -> str:
    """Devuelve el remitente si es de confianza; si no, lanza `Rejected` con el motivo."""
    settings = get_settings()
    sender = parseaddr(str(msg.get("From", "")))[1].lower()
    if sender not in allowed_senders():
        raise Rejected(f"remitente no permitido: {sender or 'desconocido'}")
    domain = sender.rsplit("@", 1)[-1]
    results = _auth_results(msg, settings.imap_authserv)
    if not results:
        raise Rejected(f"sin Authentication-Results de {settings.imap_authserv}")
    for result, props in results.get("dmarc", []):
        if result == "pass" and props.get("header.from", "").lower() == domain:
            return sender
    for result, props in results.get("dkim", []):
        signer = (props.get("header.d") or props.get("header.i", "")).lstrip("@").lower()
        if result == "pass" and signer and (signer == domain or signer.endswith("." + domain)):
            return sender
    raise Rejected(f"{sender} no pasa DMARC/DKIM alineado")


class Rejected(Exception):
    """Correo descartado (remitente o autenticación no válidos)."""


# --- Lectura de un correo ----------------------------------------------------------------


def extract_pdfs(msg: EmailMessage) -> list[tuple[str, bytes]]:
    """PDFs adjuntos (también dentro de un correo reenviado como adjunto)."""
    out: list[tuple[str, bytes]] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = part.get_filename() or ""
        ctype = part.get_content_type()
        is_pdf = ctype == "application/pdf" or (
            name.lower().endswith(".pdf") and ctype == "application/octet-stream"
        )
        if not is_pdf:
            continue
        data = part.get_payload(decode=True)
        if data and data.startswith(b"%PDF") and len(data) <= heytrade.MAX_PDF_BYTES:
            out.append((name or "adjunto.pdf", data))
        if len(out) >= MAX_PDFS_PER_MESSAGE:
            break
    return out


@dataclass
class MailOutcome:
    sender: str = ""
    subject: str = ""
    rejected: str = ""
    results: list[portfolio.DocResult] = field(default_factory=list)


def process_message(session: Session, user_id: int, raw: bytes) -> MailOutcome:
    msg = message_from_bytes(raw, policy=policy.default)
    outcome = MailOutcome(subject=str(msg.get("Subject", ""))[:200])
    try:
        outcome.sender = check_sender(msg)  # type: ignore[arg-type]
    except Rejected as exc:
        outcome.rejected = str(exc)
        return outcome
    for filename, data in extract_pdfs(msg):  # type: ignore[arg-type]
        result = portfolio.record_pdf(session, user_id, filename, data)
        if result.status == "unknown_isin":
            portfolio.store_pending(session, user_id, result, data)
        outcome.results.append(result)
    return outcome


# --- Transporte IMAP ------------------------------------------------------------------------


class MailClient(Protocol):
    def unseen(self, limit: int) -> Iterator[tuple[bytes, bytes]]: ...

    def mark_seen(self, uid: bytes) -> None: ...


class ImapClient:
    def __init__(self, host: str, port: int, user: str, password: str, folder: str):
        self.host, self.port, self.user = host, port, user
        self.password, self.folder = password, folder
        self.conn: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> Self:
        self.conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=30)
        self.conn.login(self.user, self.password)
        status, _ = self.conn.select(self.folder)
        if status != "OK":
            raise RuntimeError(f"No se pudo abrir la carpeta {self.folder}")
        return self

    def __exit__(self, *exc) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
                self.conn.logout()
            except (imaplib.IMAP4.error, OSError):  # la conexión puede haberse cerrado ya
                log.debug("Cierre del buzón con la conexión ya caída", exc_info=True)

    def unseen(self, limit: int) -> Iterator[tuple[bytes, bytes]]:
        assert self.conn is not None
        _, data = self.conn.uid("SEARCH", "UNSEEN")
        for uid in (data[0] or b"").split()[:limit]:
            _, size_data = self.conn.uid("FETCH", uid, "(RFC822.SIZE)")
            m = re.search(rb"RFC822\.SIZE (\d+)", size_data[0] if size_data else b"")
            if m and int(m[1]) > MAX_MESSAGE_BYTES:
                log.warning("Correo %s demasiado grande (%s bytes): se ignora", uid, m[1])
                self.mark_seen(uid)
                continue
            _, msg_data = self.conn.uid("FETCH", uid, "(BODY.PEEK[])")  # PEEK: no lo marca leído
            raw = next((p[1] for p in msg_data if isinstance(p, tuple)), None)
            if raw:
                yield uid, raw

    def mark_seen(self, uid: bytes) -> None:
        assert self.conn is not None
        self.conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")


def poll(session: Session, user_id: int, client: MailClient) -> MailStatus:
    """Procesa los correos sin leer. Deja sin leer los que fallen por un error transitorio."""
    messages = documents = 0
    problems: list[str] = []
    for uid, raw in client.unseen(MAX_MESSAGES_PER_RUN):
        try:
            outcome = process_message(session, user_id, raw)
        except Exception:
            session.rollback()
            log.exception("Error procesando un correo del buzón; se reintentará")
            problems.append("Error procesando un correo (se reintentará)")
            continue
        messages += 1
        if outcome.rejected:
            log.warning("Correo descartado «%s»: %s", outcome.subject, outcome.rejected)
            problems.append(f"Descartado «{outcome.subject}»: {outcome.rejected}")
        elif not outcome.results:
            log.info("Correo «%s» sin PDFs", outcome.subject)
        for r in outcome.results:
            documents += r.status == "ok"
            if r.status == "error":
                problems.append(f"{r.filename}: {r.message}")
            elif r.status == "unknown_isin":
                problems.append(f"{r.message} ({r.isin}): falta asignar ticker")
            log.info("Buzón: %s → %s (%s)", r.filename, r.status, r.message)
        client.mark_seen(uid)
    STATUS.last_run = utcnow()
    STATUS.last_error = ""
    STATUS.messages, STATUS.documents = messages, documents
    STATUS.log = (problems + STATUS.log)[:20]
    return STATUS


def target_user(session: Session) -> User | None:
    from app import auth

    settings = get_settings()
    email = auth.normalize_email(settings.imap_target_user or settings.superadmin_email)
    return session.scalar(select(User).where(User.email == email)) if email else None


def run_poll(session: Session) -> str:
    """Tarea programada (y `python -m app.cli mailbox-check`): una revisión completa."""
    if not configured():
        return "buzón sin configurar"
    settings = get_settings()
    user = target_user(session)
    if user is None:
        STATUS.last_error = "No existe la cuenta destino (IMAP_TARGET_USER o SUPERADMIN_EMAIL)"
        log.warning(STATUS.last_error)
        return STATUS.last_error
    try:
        with ImapClient(
            settings.imap_host,
            settings.imap_port,
            settings.imap_user,
            settings.imap_password,
            settings.imap_folder,
        ) as client:
            status = poll(session, user.id, client)
    except Exception as exc:
        STATUS.last_run = utcnow()
        STATUS.last_error = f"{type(exc).__name__}: {exc}"[:300]
        log.exception("Error leyendo el buzón IMAP")
        return STATUS.last_error
    return f"{status.messages} correos, {status.documents} documentos guardados"
