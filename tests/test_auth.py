"""Cuentas de usuario, seguimiento por usuario y filtros guardados."""

from urllib.parse import parse_qs, urlparse

import pytest

PASSWORD = "clave-larga-1"


def register(client, email, password=PASSWORD, **extra):
    return client.post(
        "/register", data={"email": email, "password": password, **extra}, follow_redirects=False
    )


def security_id(symbol="AAA"):
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Security

    with SessionLocal() as s:
        return s.scalar(select(Security.id).where(Security.symbol == symbol))


# --- Contraseñas y sesiones (sin app) ------------------------------------------------


def test_password_hashing():
    from app import auth

    stored = auth.hash_password("secreto-123")
    assert "secreto-123" not in stored
    assert auth.verify_password("secreto-123", stored)
    assert not auth.verify_password("otro", stored)
    assert auth.hash_password("secreto-123") != stored  # sal distinta en cada hash
    assert not auth.verify_password("x", "no-es-un-hash")


def test_first_user_adopts_legacy_watchlist(tmp_path):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app import auth
    from app.models import Base, Security, WatchlistItem

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        sec = Security(symbol="AAA", region="US", currency="USD", universes=[])
        s.add(sec)
        s.flush()
        s.add(WatchlistItem(security_id=sec.id, notes="de antes"))  # sin usuario
        s.commit()
        first = auth.create_user(s, "primero@example.com", PASSWORD)
        second = auth.create_user(s, "segundo@example.com", PASSWORD)
        owners = list(s.scalars(select(WatchlistItem.user_id)))
        assert owners == [first.id] and second.id != first.id


# --- Páginas públicas y privadas -----------------------------------------------------


def test_public_and_private_pages(anon):
    assert anon.get("/screener").status_code == 200
    assert anon.get("/security/AAA").status_code == 200
    for path, reason in (("/watchlist", "watchlist"), ("/portfolio", "portfolio")):
        r = anon.get(path, follow_redirects=False)
        assert r.status_code == 303
        target = urlparse(r.headers["location"])
        assert target.path == "/login"
        assert parse_qs(target.query) == {"next": [path], "reason": [reason]}
    assert anon.post("/watchlist/1/toggle").status_code == 401
    assert anon.post("/screener/filters", data={"name": "x"}).status_code == 401
    assert anon.post("/security/AAA/watch", data={}, follow_redirects=False).status_code == 303


def test_anonymous_follow_button_warns(anon):
    page = anon.get("/screener").text
    assert 'onclick="needLogin()"' in page and "hx-post" not in page
    assert "Necesitas estar registrado" in anon.get("/security/AAA").text
    assert "Mis filtros" not in page  # los filtros guardados son solo con sesión
    assert "Entrar" in page and "Salir" not in page


# --- Registro, entrada y salida --------------------------------------------------------


def test_register_login_logout(anon):
    r = register(anon, "  Ana@Example.COM ")  # el email se normaliza (minúsculas, sin espacios)
    assert r.status_code == 303 and r.headers["location"] == "/screener"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "stockscreener_session=" in cookie
    assert "Salir" in anon.get("/screener").text  # quedó con la sesión iniciada
    assert anon.get("/watchlist").status_code == 200

    anon.post("/logout", follow_redirects=False)
    assert anon.get("/watchlist", follow_redirects=False).status_code == 303

    bad = anon.post("/login", data={"email": "ana@example.com", "password": "mal"})
    assert bad.status_code == 400 and "incorrectos" in bad.text
    ok = anon.post(
        "/login",
        data={"email": "ANA@example.com", "password": PASSWORD, "next": "/watchlist"},
        follow_redirects=False,
    )
    assert ok.status_code == 303 and ok.headers["location"] == "/watchlist"
    assert anon.get("/watchlist").status_code == 200


@pytest.mark.parametrize(
    ("email", "password", "message"),
    [
        ("no-es-email", PASSWORD, "email válido"),
        ("corta@example.com", "1234567", "al menos 8"),
        ("larga@example.com", "x" * 201, "demasiado larga"),
    ],
)
def test_register_validation(anon, email, password, message):
    r = register(anon, email, password)
    assert r.status_code == 400 and message in r.text


def test_register_duplicate_email(anon):
    assert register(anon, "dup@example.com").status_code == 303
    r = register(anon, "DUP@example.com")
    assert r.status_code == 400 and "Ya existe una cuenta" in r.text


def test_logout_invalidates_session_token(anon):
    register(anon, "token@example.com")
    token = anon.cookies.get("stockscreener_session")
    anon.post("/logout", follow_redirects=False)
    anon.cookies.set("stockscreener_session", token)  # reutilizar la cookie ya cerrada
    assert anon.get("/watchlist", follow_redirects=False).status_code == 303


def test_login_does_not_redirect_off_site(anon):
    for evil in ("//evil.example", "https://evil.example", "/\\evil.example", "/login"):
        r = register(anon, f"redir{abs(hash(evil))}@example.com", next=evil)
        assert r.headers["location"] == "/screener", evil
        anon.post("/logout", follow_redirects=False)


def test_login_is_throttled(anon):
    register(anon, "bloqueo@example.com")
    anon.post("/logout", follow_redirects=False)
    for _ in range(8):
        anon.post("/login", data={"email": "bloqueo@example.com", "password": "mal"})
    r = anon.post("/login", data={"email": "bloqueo@example.com", "password": PASSWORD})
    assert r.status_code == 400 and "Demasiados intentos" in r.text  # ni con la clave buena


# --- Seguimiento por usuario -----------------------------------------------------------


def test_watchlist_is_per_user(client, anon):
    from fastapi.testclient import TestClient

    register(anon, "uno@example.com")
    assert "★" in anon.post(f"/watchlist/{security_id('AAA')}/toggle").text
    assert "AAA" in anon.get("/watchlist").text

    with TestClient(client.app) as other:
        register(other, "dos@example.com")
        assert "AAA" not in other.get("/watchlist").text
        assert "★" not in other.get("/screener").text  # su estrella sigue vacía
        assert "★" in other.post(f"/watchlist/{security_id('BBB.MC')}/toggle").text
        assert "BBB.MC" in other.get("/watchlist").text
    assert "BBB.MC" not in anon.get("/watchlist").text


# --- Filtros guardados -----------------------------------------------------------------

FILTERS = {
    "submitted": "1",
    "q": "texto que no se guarda",
    "sector": "Utilities",
    "yield_min": "2,5",
    "payout_max": "70",
    "region": ["EU", "US"],
    "dividend_only": "1",
    "sort": "pe_ttm",
    "desc": "0",
}


def test_saved_filters_roundtrip(anon):
    register(anon, "filtros@example.com")
    r = anon.post("/screener/filters", data={**FILTERS, "name": "  Mi   defensivo "})
    assert r.status_code == 200
    url = urlparse(r.json()["url"])
    query = parse_qs(url.query)
    assert url.path == "/screener" and query["f"]
    assert query["sector"] == ["Utilities"] and query["yield_min"] == ["2.5"]
    assert query["region"] == ["EU", "US"] and query["sort"] == ["pe_ttm"]
    assert "q" not in query and "quality_only" not in query  # sin búsqueda; lo no marcado, fuera

    page = anon.get(f"{url.path}?{url.query}")  # al aplicarlo, el selector lo marca
    assert page.status_code == 200
    assert "Mi defensivo" in page.text and "selected" in page.text
    assert 'value="2.5"' in page.text and "BBB.MC" in page.text and "CCC.L" not in page.text

    # Mismo nombre (sin distinguir mayúsculas): se actualiza en vez de duplicar
    anon.post("/screener/filters", data={**FILTERS, "yield_min": "4", "name": "MI DEFENSIVO"})
    listing = anon.get("/screener").text
    assert listing.count('<option value="/screener?submitted=1') == 1
    assert "yield_min=4&amp;" in listing


def test_saved_filters_validation_and_limits(anon):
    register(anon, "limites@example.com")
    assert anon.post("/screener/filters", data={**FILTERS, "name": "  "}).status_code == 400
    r = anon.post("/screener/filters", data={**FILTERS, "name": "x" * 41})
    assert r.status_code == 400 and "40 caracteres" in r.json()["error"]
    for i in range(30):
        assert anon.post("/screener/filters", data={**FILTERS, "name": f"f{i}"}).status_code == 200
    r = anon.post("/screener/filters", data={**FILTERS, "name": "uno más"})
    assert r.status_code == 400 and "Máximo 30" in r.json()["error"]
    assert (
        anon.post("/screener/filters", data={**FILTERS, "name": "f3"}).status_code == 200
    )  # actualizar sí


def test_saved_filters_are_private_to_their_owner(client, anon):
    from fastapi.testclient import TestClient

    register(anon, "duena@example.com")
    url = anon.post("/screener/filters", data={**FILTERS, "name": "secreto"}).json()["url"]
    filter_id = parse_qs(urlparse(url).query)["f"][0]

    with TestClient(client.app) as other:
        register(other, "intruso@example.com")
        assert "secreto" not in other.get("/screener").text
        assert other.post(f"/screener/filters/{filter_id}/delete").status_code == 404
    assert "secreto" in anon.get("/screener").text

    assert anon.post(f"/screener/filters/{filter_id}/delete").json() == {"url": "/screener"}
    assert "secreto" not in anon.get("/screener").text
