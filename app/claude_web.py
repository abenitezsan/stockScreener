"""Página «Claude»: cómo conectar Claude, claves personales, apps conectadas, historial de
consultas y análisis guardados."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, func, select

from app import oauth
from app.config import get_settings
from app.models import AiNote, McpCall, McpToken, OAuthClient, Security
from app.web import ROOT, DbSession, current_user, page_user, templates

router = APIRouter(dependencies=[Depends(current_user)])
ClaudeUser = page_user("claude")

NOTICES = {
    "revoked": "Acceso retirado.",
    "key_deleted": "Clave borrada.",
    "note_deleted": "Análisis borrado.",
}


def _page(request: Request, session, user, *, new_key: str = "", notice: str = ""):
    keys = session.scalars(
        select(McpToken)
        .where(McpToken.user_id == user.id, McpToken.kind == "key")
        .order_by(McpToken.created_at.desc())
    ).all()
    # Apps conectadas: clientes OAuth con un token de refresco vigente de este usuario
    apps = session.execute(
        select(
            OAuthClient.client_id,
            OAuthClient.name,
            func.min(McpToken.created_at),
            func.max(McpToken.last_used_at),
        )
        .join(McpToken, McpToken.client_id == OAuthClient.client_id)
        .where(McpToken.user_id == user.id, McpToken.kind == "refresh")
        .group_by(OAuthClient.client_id, OAuthClient.name)
    ).all()
    calls = session.scalars(
        select(McpCall)
        .where(McpCall.user_id == user.id)
        .order_by(McpCall.created_at.desc(), McpCall.id.desc())
        .limit(100)
    ).all()
    notes = session.execute(
        select(AiNote, Security.symbol)
        .outerjoin(Security, Security.id == AiNote.security_id)
        .where(AiNote.user_id == user.id)
        .order_by(AiNote.created_at.desc(), AiNote.id.desc())
    ).all()
    total_calls = session.scalar(select(func.count()).where(McpCall.user_id == user.id))
    return templates.TemplateResponse(
        request,
        "claude.html",
        {
            "mcp_url": oauth.resource_url(request),
            "is_https": oauth.resource_url(request).startswith("https://"),
            "enabled": get_settings().mcp_enabled,
            "allowed": oauth.user_allowed(user),
            "keys": keys,
            "apps": apps,
            "calls": calls,
            "total_calls": total_calls,
            "notes": notes,
            "new_key": new_key,
            "notice": notice,
        },
    )


@router.get("/claude", response_class=HTMLResponse)
def claude_page(request: Request, session: DbSession, user: ClaudeUser, done: str = ""):
    return _page(request, session, user, notice=NOTICES.get(done, ""))


def _back(done: str) -> RedirectResponse:
    return RedirectResponse(f"{ROOT}/claude?done={done}", status_code=303)


@router.post("/claude/keys", response_class=HTMLResponse)
def create_key(
    request: Request,
    session: DbSession,
    user: ClaudeUser,
    name: Annotated[str, Form()] = "",
):
    if not oauth.user_allowed(user):
        raise HTTPException(403, "Tu cuenta no puede conectar Claude")
    key = oauth.create_key(session, user.id, name or "Clave")
    # Se muestra una sola vez (en la base de datos solo queda su hash)
    return _page(request, session, user, new_key=key)


@router.post("/claude/keys/{prefix}/delete")
def delete_key(prefix: str, session: DbSession, user: ClaudeUser):
    session.execute(
        delete(McpToken).where(
            McpToken.user_id == user.id, McpToken.kind == "key", McpToken.prefix == prefix
        )
    )
    session.commit()
    return _back("key_deleted")


@router.post("/claude/apps/{client_id}/revoke")
def revoke_app(client_id: str, session: DbSession, user: ClaudeUser):
    session.execute(
        delete(McpToken).where(McpToken.user_id == user.id, McpToken.client_id == client_id)
    )
    session.commit()
    return _back("revoked")


@router.post("/claude/notes/{note_id}/delete")
def delete_note(note_id: int, session: DbSession, user: ClaudeUser):
    session.execute(delete(AiNote).where(AiNote.user_id == user.id, AiNote.id == note_id))
    session.commit()
    return _back("note_deleted")
