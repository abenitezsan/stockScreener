"""Flujo completo contra SQLite: universo -> tareas -> screener -> páginas."""


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
    assert "CCC.L" in page.text and "DDD" not in page.text  # por defecto, solo con dividendo
    everything = client.get("/screener", params={"submitted": "1"})
    assert "DDD" in everything.text  # casilla desmarcada: también los que no pagan
    partial = client.get(
        "/screener",
        params={"submitted": "1", "sector": "Utilities", "sort": "pe_ttm", "desc": "0"},
        headers={"HX-Request": "true"},
    )
    assert partial.status_code == 200
    assert "<html" not in partial.text
    assert "BBB.MC" in partial.text and "CCC.L" not in partial.text


def test_watchlist_and_detail(anon):
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Security

    client = anon
    assert (
        client.post(
            "/register",
            data={"email": "seguidor@example.com", "password": "clave-larga-1"},
            follow_redirects=False,
        ).status_code
        == 303
    )
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
    assert client.get("/portfolio").status_code == 200  # con sesión, la cartera es accesible


def test_universe_tags(client):
    from sqlalchemy import select

    from app import universe
    from app.db import SessionLocal
    from app.models import Security

    with SessionLocal() as s:
        universe.upsert_universe(s, "IDX", ["AAA", "bbb.mc"])
        universe.upsert_universe(s, universe.MANUAL, ["AAA"])
        universe.upsert_universe(s, "IDX", ["BBB.MC"])  # AAA sale del índice
        aaa = s.scalar(select(Security).where(Security.symbol == "AAA"))
        assert set(aaa.universes) == {"TEST", "manual"} and aaa.active
        universe.upsert_universe(s, "TEST", [])
        ddd = s.scalar(select(Security).where(Security.symbol == "DDD"))
        assert ddd.universes == [] and ddd.active is False
        s.refresh(aaa)
        assert aaa.universes == ["manual"] and aaa.active


def test_country_flag(client):
    from app.web import country_flag

    assert country_flag("Spain") == ("\U0001f1ea\U0001f1f8", "España")
    assert country_flag("Narnia") == ("", "Narnia")
    assert country_flag(None) == ("", "País desconocido")


def test_detail_explains_hard_flags(client):
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Security, Valuation

    assert "Riesgo para el dividendo" not in client.get("/security/AAA").text
    with SessionLocal() as s:
        sec = s.scalar(select(Security).where(Security.symbol == "AAA"))
        v = s.get(Valuation, sec.id)
        original = v.flags
        v.flags = [*original, "payout_alto", "fcf_negativo"]
        s.commit()
        try:
            page = client.get("/security/AAA").text
            assert "Riesgo para el dividendo" in page
            assert "Payout sobre beneficios alto." in page and "FCF negativo." in page
            assert "por encima del 70,0 %" in page  # límite del grupo general
        finally:
            v.flags = original
            s.commit()
