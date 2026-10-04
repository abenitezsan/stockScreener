"""Cuentas de usuario: contraseñas, sesiones por cookie y límite de intentos.

- Contraseñas con scrypt (biblioteca estándar, sin dependencias nuevas).
- La cookie lleva un token aleatorio; en la base de datos solo se guarda su hash SHA-256, así que
  no hay clave que configurar y cerrar sesión la invalida de verdad.
"""

import hashlib
import hmac
import re
import secrets
import time
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.config import utcnow
from app.models import User, UserSession, WatchlistItem

COOKIE = "stockscreener_session"
SESSION_DAYS = 30
MIN_PASSWORD = 8
MAX_PASSWORD = 200  # evita enviar contraseñas enormes a scrypt

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# --- Contraseñas ---------------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        computed = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(computed, bytes.fromhex(digest))


# Hash de relleno: comprobar un email inexistente tarda lo mismo que uno existente
_DUMMY_HASH = hash_password("relleno-sin-uso")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_credentials(email: str, password: str) -> str | None:
    """Mensaje de error para mostrar al usuario, o None si son válidos."""
    if len(email) > 254 or not _EMAIL.match(email):
        return "Introduce un email válido."
    if len(password) < MIN_PASSWORD:
        return f"La contraseña debe tener al menos {MIN_PASSWORD} caracteres."
    if len(password) > MAX_PASSWORD:
        return "La contraseña es demasiado larga."
    return None


# --- Usuarios ------------------------------------------------------------------------


def create_user(session: Session, email: str, password: str) -> User | None:
    """Crea la cuenta; None si el email ya existe. El primer usuario adopta el seguimiento
    anterior a las cuentas (filas sin usuario)."""
    if session.scalar(select(User.id).where(User.email == email)) is not None:
        return None
    first = session.scalar(select(User.id).limit(1)) is None
    user = User(email=email, password_hash=hash_password(password))
    session.add(user)
    session.flush()
    if first:
        session.execute(
            update(WatchlistItem).where(WatchlistItem.user_id.is_(None)).values(user_id=user.id)
        )
    session.commit()
    return user


def authenticate(session: Session, email: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.email == email))
    ok = verify_password(password, user.password_hash if user else _DUMMY_HASH)
    return user if user and ok else None


# --- Sesiones ------------------------------------------------------------------------


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(session: Session, user: User) -> str:
    """Crea una sesión y devuelve el token para la cookie."""
    now = utcnow()
    session.execute(delete(UserSession).where(UserSession.expires_at < now))  # limpieza
    token = secrets.token_urlsafe(32)
    session.add(
        UserSession(
            token_hash=_token_hash(token),
            user_id=user.id,
            expires_at=now + timedelta(days=SESSION_DAYS),
        )
    )
    session.commit()
    return token


def user_for_token(session: Session, token: str | None) -> User | None:
    if not token:
        return None
    row = session.get(UserSession, _token_hash(token))
    if row is None or _aware(row.expires_at) < utcnow():
        return None
    return session.get(User, row.user_id)


def end_session(session: Session, token: str | None) -> None:
    if token:
        session.execute(delete(UserSession).where(UserSession.token_hash == _token_hash(token)))
        session.commit()


def _aware(moment: datetime) -> datetime:
    # SQLite devuelve las fechas sin zona horaria: se guardan siempre en UTC
    return moment if moment.tzinfo else moment.replace(tzinfo=utcnow().tzinfo)


# --- Límite de intentos --------------------------------------------------------------

MAX_FAILURES = 8
WINDOW_SECONDS = 15 * 60
_failures: dict[str, list[float]] = {}


def throttled(key: str) -> bool:
    """True si ya hubo demasiados intentos fallidos recientes con esta clave (IP + email)."""
    cutoff = time.monotonic() - WINDOW_SECONDS
    recent = [t for t in _failures.get(key, []) if t > cutoff]
    if recent:
        _failures[key] = recent
    else:
        _failures.pop(key, None)
    return len(recent) >= MAX_FAILURES


def record_failure(key: str) -> None:
    _failures.setdefault(key, []).append(time.monotonic())
    if len(_failures) > 10_000:  # tope de memoria ante un ataque con emails distintos
        _failures.clear()


def clear_failures(key: str) -> None:
    _failures.pop(key, None)
