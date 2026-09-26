"""Flujo completo contra Postgres: universo -> tareas -> screener -> páginas.

Requiere TEST_DATABASE_URL (una base de datos desechable); si no está, se omite.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL no configurada"
)


@pytest.fixture(scope="module")
def client():
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
    os.environ["SCHEDULER_ENABLED"] = "false"
    os.environ["REQUEST_DELAY"] = "0"
    from sqlalchemy import text

    from app.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from app import jobs, universe
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base
    from tests.fake_provider import SPECS, FakeProvider

    schema = get_settings().db_schema
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    Base.metadata.create_all(engine)

    provider = FakeProvider()
    with SessionLocal() as session:
        universe.upsert_universe(session, "TEST", [*SPECS, "ZZZ.XX"])
        jobs.refresh_fx(session, provider)
        jobs.refresh_profiles(session, provider)
        jobs.refresh_history(session, provider)
        jobs.refresh_quotes(session, provider)
        jobs.refresh_financials(session, provider)
        jobs.recompute_valuations(session)
    with TestClient(app) as c:
        yield c


def test_pipeline_populates_valuations(client):
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import PriceHistory, Quote, Security, Valuation

    with SessionLocal() as s:
        zzz = s.scalar(select(Security).where(Security.symbol == "ZZZ.XX"))
        assert zzz.active is False  # no existe en el proveedor
        ccc = s.scalar(select(Security).where(Security.symbol == "CCC.L"))
        assert ccc.currency == "GBP" and ccc.region == "EU" and ccc.sector_group == "cyclical"
        # precios guardados en libras, no en peniques
        assert 10 < s.get(Quote, ccc.id).price < 40
        assert (
            s.scalar(select(PriceHistory.close).where(PriceHistory.security_id == ccc.id).limit(1))
            < 40
        )
        v = s.get(Valuation, ccc.id)
        assert 0.02 < v.dividend_ttm / s.get(Quote, ccc.id).price < 0.06
        assert v.fv_pe is not None  # cuentas en USD convertidas a GBP
        assert v.fair_value and v.buy_price
        bbb = s.scalar(select(Security).where(Security.symbol == "BBB.MC"))
        assert s.get(Valuation, bbb.id).fv_gordon is not None
        ddd = s.scalar(select(Security).where(Security.symbol == "DDD"))
        assert "sin_dividendo" in s.get(Valuation, ddd.id).flags


def test_screener_defaults_and_filters(client):
    page = client.get("/screener")
    assert page.status_code == 200
    assert "CCC.L" in page.text and "DDD" not in page.text  # DDD no paga dividendo
    partial = client.get(
        "/screener",
        params={"submitted": "1", "sector": "Utilities", "sort": "pe_ttm", "desc": "0"},
        headers={"HX-Request": "true"},
    )
    assert partial.status_code == 200
    assert "<html" not in partial.text
    assert "BBB.MC" in partial.text and "CCC.L" not in partial.text


def test_watchlist_and_detail(client):
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Security

    with SessionLocal() as s:
        sec_id = s.scalar(select(Security.id).where(Security.symbol == "AAA"))
    assert "★" in client.post(f"/watchlist/{sec_id}/toggle").text
    watch = client.get("/watchlist")
    assert watch.status_code == 200 and "AAA" in watch.text

    r = client.post(
        "/security/AAA/watch",
        data={"notes": "Esperar", "margin_of_safety": "20", "target_total_return": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    detail = client.get("/security/AAA")
    assert detail.status_code == 200
    assert "Esperar" in detail.text and 'value="20.0"' in detail.text
    assert client.get("/security/NOPE").status_code == 404
    assert client.get("/portfolio").status_code == 200
