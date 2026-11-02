"""Conector de Claude: OAuth 2.1 (registro dinámico + PKCE), claves personales y servidor MCP."""

import base64
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlparse

import pytest

PASSWORD = "clave-larga-1"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def _pkce():
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@pytest.fixture
def user_client(anon):
    email = f"mcp-{secrets.token_hex(4)}@example.com"
    r = anon.post("/register", data={"email": email, "password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303
    anon.email = email
    return anon


def _rpc(client, token, method, params=None, msg_id=1):
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers={"Authorization": f"Bearer {token}"})


def _call(client, token, name, args=None):
    r = _rpc(client, token, "tools/call", {"name": name, "arguments": args or {}})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    text = result["content"][0]["text"]
    return result["isError"], (text if result["isError"] else json.loads(text))


def _oauth_token(client):
    """Flujo completo como lo hace claude.ai; devuelve la respuesta del endpoint de tokens."""
    reg = client.post(
        "/oauth/register",
        json={
            "client_name": "Claude",
            "redirect_uris": [CALLBACK],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
        },
    )
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]
    verifier, challenge = _pkce()
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": CALLBACK,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "xyz",
        "scope": "mcp",
        "resource": "http://testserver/mcp",
    }
    page = client.get("/oauth/authorize", params=params)
    assert page.status_code == 200 and "Permitir" in page.text
    r = client.post(
        "/oauth/authorize", data={**params, "decision": "allow"}, follow_redirects=False
    )
    assert r.status_code == 303
    location = urlparse(r.headers["location"])
    assert location.geturl().startswith(CALLBACK)
    query = parse_qs(location.query)
    assert query["state"] == ["xyz"] and query["iss"] == ["http://testserver"]
    code = query["code"][0]
    token = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CALLBACK,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text
    # El código es de un solo uso
    again = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
    return client_id, token.json()


def test_discovery_and_unauthorized(anon):
    r = anon.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    header = r.headers["www-authenticate"]
    assert (
        'resource_metadata="http://testserver/.well-known/oauth-protected-resource/mcp"' in header
    )
    prm = anon.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == "http://testserver/mcp"
    assert prm["authorization_servers"] == ["http://testserver"]
    meta = anon.get("/.well-known/oauth-authorization-server").json()
    assert meta["issuer"] == "http://testserver"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["registration_endpoint"] == "http://testserver/oauth/register"
    assert anon.get("/.well-known/openid-configuration").json() == meta
    bad = anon.post("/mcp", json={}, headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    assert anon.get("/mcp").status_code == 405


def test_register_validation(anon):
    r = anon.post("/oauth/register", json={"redirect_uris": ["http://evil.example.com/cb"]})
    assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"
    r = anon.post("/oauth/register", json={"redirect_uris": ["http://localhost:3118/callback"]})
    assert r.status_code == 201
    r = anon.post(
        "/oauth/register",
        json={"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "client_secret_post"},
    )
    assert r.status_code == 201 and r.json()["client_secret"]


def test_authorize_requires_login_and_valid_client(anon):
    reg = anon.post("/oauth/register", json={"redirect_uris": [CALLBACK]}).json()
    _, challenge = _pkce()
    params = {
        "response_type": "code",
        "client_id": reg["client_id"],
        "redirect_uri": CALLBACK,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    r = anon.get("/oauth/authorize", params=params, follow_redirects=False)
    assert r.status_code == 303 and "/login?next=%2Foauth%2Fauthorize" in r.headers["location"]
    # Redirección no registrada: error en pantalla, nunca se redirige
    r = anon.get(
        "/oauth/authorize", params={**params, "redirect_uri": "https://evil.example.com/cb"}
    )
    assert r.status_code == 400 and "no está registrada" in r.text
    r = anon.get("/oauth/authorize", params={**params, "client_id": "desconocido"})
    assert r.status_code == 400
    # Sin PKCE: se devuelve el error al cliente
    r = anon.get(
        "/oauth/authorize",
        params={**params, "code_challenge_method": "plain"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "error=invalid_request" in r.headers["location"]


def test_deny_and_wrong_verifier(user_client):
    reg = user_client.post("/oauth/register", json={"redirect_uris": [CALLBACK]}).json()
    _, challenge = _pkce()
    params = {
        "response_type": "code",
        "client_id": reg["client_id"],
        "redirect_uri": CALLBACK,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "s1",
    }
    r = user_client.post(
        "/oauth/authorize", data={**params, "decision": "deny"}, follow_redirects=False
    )
    assert "error=access_denied" in r.headers["location"] and "state=s1" in r.headers["location"]
    r = user_client.post(
        "/oauth/authorize", data={**params, "decision": "allow"}, follow_redirects=False
    )
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    r = user_client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": reg["client_id"],
            "code_verifier": _pkce()[0],
        },
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_oauth_flow_tools_and_history(user_client):
    client_id, tok = _oauth_token(user_client)
    access = tok["access_token"]
    assert tok["token_type"] == "Bearer" and tok["expires_in"] == 3600

    init = _rpc(
        user_client,
        access,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}},
    ).json()["result"]
    assert init["protocolVersion"] == "2025-06-18"
    assert init["capabilities"]["tools"] and "dividendos" in init["instructions"]
    note = user_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={"Authorization": f"Bearer {access}"},
    )
    assert note.status_code == 202
    tools = _rpc(user_client, access, "tools/list").json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"screener", "ficha_valor", "cartera", "guardar_analisis"} <= names
    assert all(t["inputSchema"]["type"] == "object" for t in tools)

    # Datos: screener, ficha, cartera vacía, seguimiento
    err, data = _call(user_client, access, "screener", {"limite": 2})
    assert not err and len(data["resultados"]) <= 2 and data["total_por_sector"]
    err, data = _call(user_client, access, "buscar_valor", {"texto": "AAA"})
    assert not err and data["resultados"][0]["ticker"] == "AAA"
    err, data = _call(user_client, access, "ficha_valor", {"ticker": "aaa"})
    assert not err and data["ticker"] == "AAA" and "valoracion" in data
    err, data = _call(user_client, access, "cartera")
    assert not err and data["divisa"] == "EUR"
    err, msg = _call(user_client, access, "ficha_valor", {"ticker": "NOEXISTE"})
    assert err and "No encuentro" in msg
    err, msg = _call(user_client, access, "screener", {"orden": "inventado"})
    assert err

    # Guardar y leer análisis
    err, saved = _call(
        user_client,
        access,
        "guardar_analisis",
        {"titulo": "Tesis AAA", "contenido": "# Compra\nYield alta", "ticker": "AAA"},
    )
    assert not err and saved["guardado"] and saved["ticker"] == "AAA"
    err, notes = _call(user_client, access, "historial_analisis", {"ticker": "AAA"})
    assert notes["analisis"][0]["titulo"] == "Tesis AAA"
    assert "Tesis AAA" in user_client.get("/security/AAA").text

    # Historial en la base de datos y en la página «Claude»
    page = user_client.get("/claude")
    assert page.status_code == 200
    assert "guardar_analisis" in page.text and "Tesis AAA" in page.text
    assert "http://testserver/mcp" in page.text and ">Claude<" in page.text

    # Lote JSON-RPC, método desconocido
    batch = user_client.post(
        "/mcp",
        json=[
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 2, "method": "nada"},
        ],
        headers={"Authorization": f"Bearer {access}"},
    ).json()
    assert batch[0]["result"] == {} and batch[1]["error"]["code"] == -32601

    # Refresco con rotación
    r = user_client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": client_id,
        },
    )
    assert r.status_code == 200
    new = r.json()
    assert new["refresh_token"] != tok["refresh_token"]
    assert _rpc(user_client, new["access_token"], "ping").status_code == 200
    bad = user_client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": "x", "client_id": client_id},
    )
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"

    # Desconectar la app desde la página retira todos sus tokens
    r = user_client.post(f"/claude/apps/{client_id}/revoke", follow_redirects=False)
    assert r.status_code == 303
    assert _rpc(user_client, new["access_token"], "ping").status_code == 401


def test_personal_key_and_isolation(user_client):
    from fastapi.testclient import TestClient

    r = user_client.post("/claude/keys", data={"name": "Claude Code"})
    assert r.status_code == 200
    key = r.text.split('value="ssk_')[1].split('"')[0]
    key = "ssk_" + key
    assert _rpc(user_client, key, "ping").status_code == 200
    err, _ = _call(user_client, key, "cartera")
    assert not err

    # Otro usuario solo ve sus propios análisis
    _call(user_client, key, "guardar_analisis", {"titulo": "Privado", "contenido": "x"})
    other = TestClient(user_client.app)
    other.post(
        "/register",
        data={"email": f"otro-{secrets.token_hex(4)}@example.com", "password": PASSWORD},
    )
    r = other.post("/claude/keys", data={"name": "otra"})
    other_key = "ssk_" + r.text.split('value="ssk_')[1].split('"')[0]
    err, notes = _call(other, other_key, "historial_analisis")
    assert notes.get("analisis", []) == []

    prefix = key[:10]
    user_client.post(f"/claude/keys/{prefix}/delete")
    assert _rpc(user_client, key, "ping").status_code == 401


def test_claude_page_requires_login(anon):
    r = anon.get("/claude", follow_redirects=False)
    assert r.status_code == 303 and "reason=claude" in r.headers["location"]
