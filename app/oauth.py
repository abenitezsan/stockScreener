"""Autorización del conector de Claude: servidor OAuth 2.1 mínimo y claves personales.

Claude (claude.ai, escritorio, móvil y Claude Code) se conecta al servidor MCP (`/mcp`):

1. Pide `/mcp` sin token y recibe 401 con la dirección de los metadatos del recurso
   (`/.well-known/oauth-protected-resource`), que apuntan a este mismo servidor de autorización.
2. Se registra solo (registro dinámico de cliente, RFC 7591) en `/oauth/register`.
3. Abre `/oauth/authorize` en el navegador: el usuario entra con su cuenta de la app y pulsa
   «Permitir». Se usa PKCE (S256) obligatorio.
4. Cambia el código por un token de acceso (1 hora) y otro de refresco (180 días, rotatorio) en
   `/oauth/token`. Claude los renueva solo: basta con autorizar una vez.

Alternativa sin OAuth: una clave personal (`ssk_...`) creada en la página «Claude» o con
`python -m app.cli mcp-key`, enviada como `Authorization: Bearer ssk_...` (Claude Code, o
conectores con cabeceras fijas). En la base de datos solo se guarda el hash SHA-256 de cada token.
"""

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated
from urllib.parse import unquote, urlencode, urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import auth
from app.config import get_settings, utcnow
from app.models import McpToken, OAuthClient, OAuthCode, User
from app.web import ROOT, CurrentUser, DbSession, LoginRequired, templates

ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=180)
CODE_TTL = timedelta(minutes=10)
ROTATION_GRACE = timedelta(seconds=120)  # refresh ya rotado que aún se acepta (reintentos)
SCOPE = "mcp"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
KEY_PREFIX = "ssk_"
LOOPBACK = {"localhost", "127.0.0.1", "::1"}

router = APIRouter()


# --- Utilidades ----------------------------------------------------------------------


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def public_base(request: Request) -> str:
    """URL pública de la app (con su subruta): la configurada o la de la propia petición."""
    configured = get_settings().public_url.strip().rstrip("/")
    if configured:
        return configured
    host = request.headers.get("host") or request.url.netloc
    return f"{request.url.scheme}://{host}{ROOT}"


def resource_url(request: Request) -> str:
    return f"{public_base(request)}/mcp"


def user_allowed(user: User) -> bool:
    allowed = {auth.normalize_email(e) for e in get_settings().mcp_users.split(",") if e.strip()}
    return not allowed or user.email in allowed or auth.is_superadmin(user)


def _aware(moment):
    return auth._aware(moment) if moment is not None else None


def _no_store(data: dict, status: int = 200, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        data,
        status_code=status,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache", **(headers or {})},
    )


def _oauth_error(error: str, description: str, status: int = 400) -> JSONResponse:
    headers = {"WWW-Authenticate": 'Basic realm="oauth"'} if status == 401 else None
    return _no_store({"error": error, "error_description": description}, status, headers)


# Límite sencillo de registros de cliente por IP (en memoria, como el de entradas)
_registrations: dict[str, list[float]] = {}
MAX_REGISTRATIONS = 20
REGISTRATION_WINDOW = 3600


def _registration_throttled(ip: str) -> bool:
    cutoff = time.monotonic() - REGISTRATION_WINDOW
    recent = [t for t in _registrations.get(ip, []) if t > cutoff]
    _registrations[ip] = recent
    if len(_registrations) > 10_000:
        _registrations.clear()
    if len(recent) >= MAX_REGISTRATIONS:
        return True
    recent.append(time.monotonic())
    return False


# --- Metadatos (descubrimiento) ------------------------------------------------------


def protected_resource_metadata(request: Request) -> dict:
    return {
        "resource": resource_url(request),
        "authorization_servers": [public_base(request)],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
        "resource_name": "Stock Screener",
    }


def authorization_server_metadata(request: Request) -> dict:
    base = public_base(request)
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "scopes_supported": [SCOPE],
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": [
            "none",
            "client_secret_post",
            "client_secret_basic",
        ],
        "revocation_endpoint_auth_methods_supported": [
            "none",
            "client_secret_post",
            "client_secret_basic",
        ],
        "authorization_response_iss_parameter_supported": True,
    }


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
def well_known_resource(request: Request):
    return JSONResponse(protected_resource_metadata(request))


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/openid-configuration")
def well_known_server(request: Request):
    return JSONResponse(authorization_server_metadata(request))


# --- Registro dinámico de clientes (RFC 7591) ----------------------------------------


def _valid_redirect(uri: str) -> bool:
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if parts.fragment or not parts.netloc:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and (parts.hostname or "") in LOOPBACK


def redirect_matches(registered: list[str], uri: str) -> bool:
    """Coincidencia exacta; en las direcciones de bucle local (Claude Code) se ignora el puerto."""
    if uri in registered:
        return True
    try:
        given = urlsplit(uri)
    except ValueError:
        return False
    if given.scheme != "http" or given.hostname not in LOOPBACK:
        return False
    for reg in registered:
        r = urlsplit(reg)
        if (r.scheme, r.hostname, r.path, r.query) == (
            given.scheme,
            given.hostname,
            given.path,
            given.query,
        ):
            return True
    return False


def create_client(
    session: Session, name: str, redirect_uris: list[str], confidential: bool = False
) -> tuple[OAuthClient, str | None]:
    """Da de alta un cliente; devuelve también su secreto (solo si es confidencial)."""
    secret = secrets.token_urlsafe(32) if confidential else None
    client = OAuthClient(
        client_id="ssc_" + secrets.token_urlsafe(18),
        secret_hash=token_hash(secret) if secret else None,
        name=name[:200] or "Cliente MCP",
        redirect_uris=redirect_uris,
    )
    session.add(client)
    session.commit()
    return client, secret


def _prune_clients(session: Session) -> None:
    """Borra los clientes registrados hace más de un día que nunca llegaron a tener tokens."""
    cutoff = utcnow() - timedelta(days=1)
    used = select(McpToken.client_id).where(McpToken.client_id.is_not(None))
    session.execute(
        delete(OAuthClient).where(
            OAuthClient.created_at < cutoff, OAuthClient.client_id.not_in(used)
        )
    )


@router.post("/oauth/register")
async def register_client(request: Request):
    from app.db import SessionLocal

    ip = request.client.host if request.client else "?"
    if _registration_throttled(ip):
        return _oauth_error(
            "invalid_client_metadata", "Demasiados registros; prueba más tarde", 429
        )
    try:
        meta = await request.json()
    except ValueError:
        return _oauth_error("invalid_client_metadata", "El cuerpo debe ser JSON")
    if not isinstance(meta, dict):
        return _oauth_error("invalid_client_metadata", "El cuerpo debe ser un objeto JSON")
    uris = meta.get("redirect_uris")
    if (
        not isinstance(uris, list)
        or not uris
        or len(uris) > 10
        or not all(isinstance(u, str) and len(u) <= 2000 and _valid_redirect(u) for u in uris)
    ):
        return _oauth_error(
            "invalid_redirect_uri", "redirect_uris debe ser una lista de URLs https (o localhost)"
        )
    method = meta.get("token_endpoint_auth_method", "none")
    if method not in ("none", "client_secret_post", "client_secret_basic"):
        return _oauth_error("invalid_client_metadata", "token_endpoint_auth_method no admitido")
    grants = meta.get("grant_types") or ["authorization_code", "refresh_token"]
    if not isinstance(grants, list) or not set(grants) <= {"authorization_code", "refresh_token"}:
        return _oauth_error("invalid_client_metadata", "grant_types no admitidos")
    name = meta.get("client_name")
    name = name.strip() if isinstance(name, str) and name.strip() else "Cliente MCP"
    with SessionLocal() as session:
        _prune_clients(session)
        client, secret = create_client(session, name, uris, confidential=method != "none")
    body = {
        "client_id": client.client_id,
        "client_id_issued_at": int(time.time()),
        "client_name": client.name,
        "redirect_uris": uris,
        "grant_types": grants,
        "response_types": ["code"],
        "token_endpoint_auth_method": method,
        "scope": SCOPE,
    }
    if secret:
        body |= {"client_secret": secret, "client_secret_expires_at": 0}
    return _no_store(body, 201)


# --- Autorización (pantalla de consentimiento) -----------------------------------------


@dataclass
class AuthRequest:
    client: OAuthClient
    redirect_uri: str
    state: str
    code_challenge: str
    resource: str


def _error_page(request: Request, message: str, status: int = 400):
    return templates.TemplateResponse(
        request, "oauth_authorize.html", {"error": message}, status_code=status
    )


def _redirect_error(redirect_uri: str, state: str, error: str, request: Request, description=""):
    params = {"error": error, "iss": public_base(request)}
    if description:
        params["error_description"] = description
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=303)


def _check_request(request: Request, session: Session, p: dict) -> AuthRequest | HTMLResponse:
    """Valida los parámetros de /oauth/authorize. Si el cliente o la redirección no son
    válidos se muestra un error (nunca se redirige a una URL no registrada)."""
    client = session.get(OAuthClient, p.get("client_id") or "")
    if client is None:
        return _error_page(request, "Cliente desconocido: vuelve a añadir el conector en Claude.")
    redirect_uri = p.get("redirect_uri") or ""
    if not redirect_uri and len(client.redirect_uris) == 1:
        redirect_uri = client.redirect_uris[0]
    if not redirect_matches(client.redirect_uris, redirect_uri):
        return _error_page(request, "La dirección de vuelta no está registrada para este cliente.")
    state = p.get("state") or ""
    if p.get("response_type") != "code":
        return _redirect_error(redirect_uri, state, "unsupported_response_type", request)
    challenge = p.get("code_challenge") or ""
    if p.get("code_challenge_method") != "S256" or not 43 <= len(challenge) <= 128:
        return _redirect_error(
            redirect_uri, state, "invalid_request", request, "PKCE con S256 es obligatorio"
        )
    resource = p.get("resource") or ""
    if resource and not resource.rstrip("/").startswith(public_base(request)):
        return _redirect_error(redirect_uri, state, "invalid_target", request)
    return AuthRequest(client, redirect_uri, state, challenge, resource)


@router.get("/oauth/authorize", response_class=HTMLResponse)
def authorize_page(request: Request, session: DbSession, user: CurrentUser):
    params = dict(request.query_params)
    checked = _check_request(request, session, params)
    if not isinstance(checked, AuthRequest):
        return checked
    if user is None:
        raise LoginRequired(f"/oauth/authorize?{request.url.query}", "oauth")
    if not get_settings().mcp_enabled or not user_allowed(user):
        return _redirect_error(
            checked.redirect_uri, checked.state, "access_denied", request, "Usuario no autorizado"
        )
    response = templates.TemplateResponse(
        request,
        "oauth_authorize.html",
        {
            "client": checked.client,
            "params": params,
            "redirect_host": urlsplit(checked.redirect_uri).hostname,
        },
    )
    # Que otra web no pueda incrustar la pantalla de «Permitir» (clickjacking)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return response


@router.post("/oauth/authorize")
async def authorize_decision(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    decision: Annotated[str, Form()] = "",
):
    # La cookie de sesión es SameSite=Lax: un formulario de otro sitio no la lleva, así que
    # otro sitio no puede autorizar en nombre del usuario.
    form = await request.form()
    params = {k: str(v) for k, v in form.items() if k != "decision"}
    checked = _check_request(request, session, params)
    if not isinstance(checked, AuthRequest):
        return checked
    if user is None:
        raise LoginRequired(f"/oauth/authorize?{urlencode(params)}", "oauth")
    if decision != "allow" or not get_settings().mcp_enabled or not user_allowed(user):
        return _redirect_error(checked.redirect_uri, checked.state, "access_denied", request)
    code = secrets.token_urlsafe(32)
    session.execute(delete(OAuthCode).where(OAuthCode.expires_at < utcnow()))
    session.add(
        OAuthCode(
            code_hash=token_hash(code),
            client_id=checked.client.client_id,
            user_id=user.id,
            redirect_uri=checked.redirect_uri,
            code_challenge=checked.code_challenge,
            resource=checked.resource or None,
            expires_at=utcnow() + CODE_TTL,
        )
    )
    session.commit()
    query = {"code": code, "iss": public_base(request)}
    if checked.state:
        query["state"] = checked.state
    sep = "&" if "?" in checked.redirect_uri else "?"
    return RedirectResponse(f"{checked.redirect_uri}{sep}{urlencode(query)}", status_code=303)


# --- Tokens --------------------------------------------------------------------------


def _client_auth(request: Request, session: Session, form: dict) -> OAuthClient | JSONResponse:
    """Identifica al cliente (público con client_id, o confidencial con su secreto)."""
    client_id, secret = form.get("client_id"), form.get("client_secret")
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode()
            basic_id, _, basic_secret = decoded.partition(":")
            client_id, secret = unquote(basic_id), unquote(basic_secret)
        except (ValueError, UnicodeDecodeError):
            return _oauth_error("invalid_client", "Cabecera Basic no válida", 401)
    client = session.get(OAuthClient, client_id or "")
    if client is None:
        return _oauth_error("invalid_client", "Cliente desconocido", 401)
    if client.secret_hash and not (
        secret and hmac.compare_digest(token_hash(secret), client.secret_hash)
    ):
        return _oauth_error("invalid_client", "Secreto de cliente incorrecto", 401)
    return client


def _issue(session: Session, client: OAuthClient, user_id: int) -> dict:
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    now = utcnow()
    # Limpieza de tokens caducados
    session.execute(delete(McpToken).where(McpToken.expires_at < now))
    session.add_all(
        [
            McpToken(
                token_hash=token_hash(access),
                kind="access",
                user_id=user_id,
                client_id=client.client_id,
                expires_at=now + ACCESS_TTL,
            ),
            McpToken(
                token_hash=token_hash(refresh),
                kind="refresh",
                user_id=user_id,
                client_id=client.client_id,
                expires_at=now + REFRESH_TTL,
            ),
        ]
    )
    session.commit()
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": int(ACCESS_TTL.total_seconds()),
        "refresh_token": refresh,
        "scope": SCOPE,
    }


def _pkce_ok(verifier: str, challenge: str) -> bool:
    if not 43 <= len(verifier) <= 128:
        return False
    digest = hashlib.sha256(verifier.encode("ascii", "ignore")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return hmac.compare_digest(computed, challenge)


@router.post("/oauth/token")
async def token(request: Request):
    from app.db import SessionLocal

    form = {k: str(v) for k, v in (await request.form()).items()}
    grant = form.get("grant_type")
    with SessionLocal() as session:
        client = _client_auth(request, session, form)
        if isinstance(client, JSONResponse):
            return client
        now = utcnow()
        if grant == "authorization_code":
            row = session.get(OAuthCode, token_hash(form.get("code", "")))
            if row is None or row.client_id != client.client_id or _aware(row.expires_at) < now:
                return _oauth_error("invalid_grant", "Código no válido o caducado")
            session.delete(row)  # de un solo uso
            session.commit()
            if form.get("redirect_uri") and form["redirect_uri"] != row.redirect_uri:
                return _oauth_error("invalid_grant", "redirect_uri no coincide")
            if not _pkce_ok(form.get("code_verifier", ""), row.code_challenge):
                return _oauth_error("invalid_grant", "code_verifier incorrecto")
            if session.get(User, row.user_id) is None:
                return _oauth_error("invalid_grant", "El usuario ya no existe")
            return _no_store(_issue(session, client, row.user_id))
        if grant == "refresh_token":
            row = session.get(McpToken, token_hash(form.get("refresh_token", "")))
            if (
                row is None
                or row.kind != "refresh"
                or row.client_id != client.client_id
                or _aware(row.expires_at) < now
                or (row.rotated_at is not None and _aware(row.rotated_at) + ROTATION_GRACE < now)
            ):
                return _oauth_error("invalid_grant", "Token de refresco no válido o caducado")
            user = session.get(User, row.user_id)
            if user is None or not user_allowed(user) or not get_settings().mcp_enabled:
                return _oauth_error("invalid_grant", "Acceso retirado")
            if row.rotated_at is None:
                row.rotated_at = now
                # El refresco rotado deja de valer pasado el margen
                row.expires_at = now + ROTATION_GRACE
            return _no_store(_issue(session, client, row.user_id))
        return _oauth_error("unsupported_grant_type", "grant_type no admitido")


@router.post("/oauth/revoke")
async def revoke(request: Request):
    from app.db import SessionLocal

    form = {k: str(v) for k, v in (await request.form()).items()}
    with SessionLocal() as session:
        client = _client_auth(request, session, form)
        if isinstance(client, JSONResponse):
            return client
        row = session.get(McpToken, token_hash(form.get("token", "")))
        if row is not None and row.client_id == client.client_id:
            session.delete(row)
            session.commit()
    return _no_store({})


# --- Validación del token en /mcp y claves personales --------------------------------


@dataclass
class Caller:
    user: User
    client: str  # nombre del cliente OAuth o de la clave personal


def caller_for(session: Session, header: str | None) -> Caller | None:
    if not header or not header.lower().startswith("bearer "):
        return None
    value = header[7:].strip()
    if not value:
        return None
    row = session.get(McpToken, token_hash(value))
    now = utcnow()
    if row is None or row.kind not in ("access", "key"):
        return None
    if row.expires_at is not None and _aware(row.expires_at) < now:
        return None
    user = session.get(User, row.user_id)
    if user is None or not user_allowed(user):
        return None
    last = _aware(row.last_used_at)
    if last is None or now - last > timedelta(minutes=5):
        row.last_used_at = now
        if row.kind == "access" and row.client_id:
            # Para «Apps conectadas»: la última vez que se usó el cliente
            session.execute(
                McpToken.__table__.update()
                .where(McpToken.client_id == row.client_id, McpToken.kind == "refresh")
                .where(McpToken.user_id == row.user_id, McpToken.rotated_at.is_(None))
                .values(last_used_at=now)
            )
        session.commit()
    if row.kind == "key":
        name = f"Clave «{row.name}»" if row.name else "Clave personal"
    else:
        client = session.get(OAuthClient, row.client_id) if row.client_id else None
        name = client.name if client else "OAuth"
    return Caller(user, name)


def create_key(session: Session, user_id: int, name: str) -> str:
    """Crea una clave personal (sin caducidad) y la devuelve; solo se muestra esta vez."""
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    session.add(
        McpToken(
            token_hash=token_hash(key),
            kind="key",
            user_id=user_id,
            name=(name.strip() or "Clave")[:100],
            prefix=key[:10],
        )
    )
    session.commit()
    return key
