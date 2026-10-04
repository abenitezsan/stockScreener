"""Gestión de usuarios restringida al superadmin (su email viene de SUPERADMIN_EMAIL)."""

from urllib.parse import parse_qs, urlparse

import pytest

PASSWORD = "clave-larga-1"
ADMIN = "admin@example.com"  # SUPERADMIN_EMAIL en conftest, con otras mayúsculas y espacios


def login(client, email, password=PASSWORD):
    return client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )


def make_user(email, password=PASSWORD):
    from app import auth
    from app.db import SessionLocal

    with SessionLocal() as s:
        return auth.create_user(s, email, password)


def user_id(email):
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import User

    with SessionLocal() as s:
        return s.scalar(select(User.id).where(User.email == email))


@pytest.fixture
def admin(anon):
    """Cliente con la sesión del superadmin (la cuenta se crea con la CLI, no con el registro)."""
    if user_id(ADMIN) is None:
        make_user(ADMIN)
    assert login(anon, ADMIN).status_code == 303
    return anon


def test_superadmin_email_is_reserved_for_public_signup(anon):
    r = anon.post("/register", data={"email": "ADMIN@example.com", "password": PASSWORD})
    assert r.status_code == 400 and "Ya existe una cuenta" in r.text
    assert "stockscreener_session" not in r.headers.get("set-cookie", "")  # sin sesión nueva
    assert "Salir" not in anon.get("/screener").text


def test_admin_pages_are_restricted(anon, admin):
    from fastapi.testclient import TestClient

    # Sin sesión: redirige al login. Con sesión de usuario normal: 404 (no se revela la ruta)
    with TestClient(anon.app) as visitor:
        r = visitor.get("/admin/users", follow_redirects=False)
        assert r.status_code == 303
        target = urlparse(r.headers["location"])
        assert target.path == "/login" and parse_qs(target.query)["reason"] == ["admin"]
        assert visitor.post("/admin/users", data={}, follow_redirects=False).status_code == 303

    make_user("normal@example.com")
    with TestClient(anon.app) as normal:
        assert login(normal, "normal@example.com").status_code == 303
        assert "/admin/users" not in normal.get("/screener").text  # sin enlace en el menú
        assert normal.get("/admin/users").status_code == 404
        victim = user_id("normal@example.com")
        for path in (
            f"/admin/users/{victim}/password",
            f"/admin/users/{victim}/delete",
            "/admin/users",
        ):
            assert (
                normal.post(path, data={"password": PASSWORD, "email": "x@example.com"}).status_code
                == 404
            )
        assert user_id("normal@example.com") == victim  # nada cambió

    page = admin.get("/admin/users")
    assert page.status_code == 200 and "normal@example.com" in page.text
    assert 'href="/admin/users"' in admin.get("/screener").text


def test_admin_lists_usage(admin):
    from fastapi.testclient import TestClient

    make_user("uso@example.com")
    with TestClient(admin.app) as u:
        login(u, "uso@example.com")
        u.post("/screener/filters", data={"name": "uno", "submitted": "1", "sort": "yield_ttm"})
    page = admin.get("/admin/users").text
    row = page[page.index("uso@example.com") :]
    row = row[: row.index("</article>")]
    assert "0 en seguimiento" in row and "1 filtros guardados" in row and "1 sesión activa" in row


def test_admin_creates_users(admin):
    r = admin.post(
        "/admin/users",
        data={"email": "Nuevo@Example.com", "password": PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/admin/users?done=created"
    assert "Usuario creado." in admin.get("/admin/users?done=created").text
    assert user_id("nuevo@example.com") is not None

    dup = admin.post("/admin/users", data={"email": "nuevo@example.com", "password": PASSWORD})
    assert dup.status_code == 400 and "Ya existe una cuenta" in dup.text
    short = admin.post("/admin/users", data={"email": "otro@example.com", "password": "corta"})
    assert short.status_code == 400 and "al menos 8" in short.text
    assert user_id("otro@example.com") is None


def test_admin_resets_password_and_closes_sessions(admin):
    from fastapi.testclient import TestClient

    make_user("olvido@example.com")
    with TestClient(admin.app) as u:
        login(u, "olvido@example.com")
        assert u.get("/watchlist", follow_redirects=False).status_code == 200
        target = user_id("olvido@example.com")

        bad = admin.post(f"/admin/users/{target}/password", data={"password": "corta"})
        assert bad.status_code == 400 and "al menos 8" in bad.text
        assert u.get("/watchlist", follow_redirects=False).status_code == 200  # sigue dentro

        r = admin.post(
            f"/admin/users/{target}/password",
            data={"password": "contraseña-nueva-9"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert u.get("/watchlist", follow_redirects=False).status_code == 303  # sesión cerrada
    with TestClient(admin.app) as again:
        assert login(again, "olvido@example.com").status_code == 400  # la antigua ya no vale
        assert login(again, "olvido@example.com", "contraseña-nueva-9").status_code == 303

    # «Cerrar sesiones» por separado
    assert admin.post(f"/admin/users/{target}/sessions", follow_redirects=False).status_code == 303
    assert admin.post("/admin/users/999999/sessions").status_code == 404


def test_admin_deletes_users_with_their_data(admin):
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.models import SavedFilter, WatchlistItem

    make_user("borrar@example.com")
    with TestClient(admin.app) as u:
        login(u, "borrar@example.com")
        u.post("/watchlist/1/toggle")
        u.post("/screener/filters", data={"name": "mío", "submitted": "1"})
    target = user_id("borrar@example.com")

    r = admin.post(f"/admin/users/{target}/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/users?done=deleted"
    assert user_id("borrar@example.com") is None
    with SessionLocal() as s:  # el seguimiento y los filtros caen en cascada
        assert s.scalar(select(func.count()).where(WatchlistItem.user_id == target)) == 0
        assert s.scalar(select(func.count()).where(SavedFilter.user_id == target)) == 0
    with TestClient(admin.app) as again:
        assert login(again, "borrar@example.com").status_code == 400
    assert admin.post("/admin/users/999999/delete").status_code == 404


def test_superadmin_cannot_delete_own_account(admin):
    me = user_id(ADMIN)
    r = admin.post(f"/admin/users/{me}/delete")
    assert r.status_code == 400 and "No puedes borrar" in r.text
    assert user_id(ADMIN) == me
    assert (
        "Borrar usuario"
        not in admin.get("/admin/users").text.split(ADMIN)[1].split("</article>")[0]
    )


def test_cli_set_password_creates_and_updates():
    from app import cli
    from app.db import SessionLocal

    with SessionLocal() as s:
        assert (
            cli.set_user_password(s, " CLI@Example.com ", PASSWORD)
            == "Cuenta creada: cli@example.com"
        )
        assert "Contraseña cambiada" in cli.set_user_password(s, "cli@example.com", "otra-clave-77")
        with pytest.raises(ValueError, match="al menos 8"):
            cli.set_user_password(s, "cli@example.com", "corta")
        with pytest.raises(ValueError, match="email válido"):
            cli.set_user_password(s, "no-es-email", PASSWORD)
    from app import auth

    with SessionLocal() as s:
        assert auth.authenticate(s, "cli@example.com", "otra-clave-77") is not None
        assert auth.authenticate(s, "cli@example.com", PASSWORD) is None
