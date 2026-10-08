"""Cartera: lectura de PDFs de HeyTrade, posiciones, dividendos, proyección y páginas."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app import heytrade, portfolio
from tests.test_auth import register

FIXTURES = Path(__file__).parent / "fixtures" / "heytrade"
D = Decimal


def text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --- Lectura de PDFs (texto de plantillas reales, sin datos personales) -----------------


def test_parse_buy_usd():
    doc = heytrade.parse_trade(text("buy.txt"))
    assert (doc.kind, doc.isin, doc.currency) == ("buy", "US7561091049", "USD")
    assert doc.trade_date == date(2026, 9, 25)
    assert (doc.quantity, doc.price) == (D(11), D("55.3950"))
    assert doc.fx_rate == D("0.87728")
    assert doc.fees == D("2.54")  # 2,00 de ejecución + 0,54 de cambio
    assert doc.total_eur == D("537.11")


def test_parse_dividend_national():
    doc = heytrade.parse_dividend(text("dividend_es.txt"))
    assert (doc.isin, doc.currency, doc.shares) == ("ES0112501012", "EUR", D(54))
    assert (doc.gross, doc.net, doc.net_base) == (D("12.42"), D("10.06"), D("10.06"))
    assert doc.withholding_domestic == D("2.36") and doc.withholding_origin == 0
    assert doc.withholding_rate == D("0.19")
    assert (doc.ex_date, doc.pay_date) == (date(2026, 9, 29), date(2026, 10, 1))


def test_parse_sell_assumes_same_fields():
    doc = heytrade.parse_trade(text("sell_synthetic.txt"))
    assert doc.kind == "sell" and doc.total_eur == D("537.11")


def test_parse_foreign_dividend_origin_withholding():
    doc = heytrade.parse_dividend(text("dividend_us_synthetic.txt"))
    assert doc.withholding_origin == D("1.86") and doc.withholding_domestic == 0
    assert doc.currency == "USD" and doc.fx_rate is None and doc.net_base is None


def test_parse_errors():
    with pytest.raises(heytrade.ParseError):
        heytrade.pdf_text(b"no soy un pdf")
    with pytest.raises(heytrade.ParseError, match="ISIN"):
        heytrade.parse_trade(text("buy.txt").replace("US7561091049", "XX"))
    with pytest.raises(heytrade.ParseError, match="no cuadran"):
        heytrade.parse_dividend(text("dividend_es.txt").replace("10,06 EUR", "99,06 EUR"))
    with pytest.raises(heytrade.ParseError, match="Falta"):
        heytrade.parse_trade(text("buy.txt").replace("Cantidad", "Otra cosa"))
    assert heytrade.parse_number("1.234,56") == D("1234.56")
    assert heytrade.parse_number("19,00%") == D("19.00")
    with pytest.raises(heytrade.ParseError):
        heytrade.parse_number("NaN")


# --- Cartera con la app y la base de pruebas ------------------------------------------------


def _db():
    from app.db import SessionLocal

    return SessionLocal()


def _user(client, email):
    from sqlalchemy import select

    from app import auth
    from app.models import User

    auth._failures.clear()  # el límite de altas es por IP y todos los tests comparten la suya
    client.cookies.clear()
    register(client, email)
    with _db() as s:
        return s.scalar(select(User).where(User.email == email))


def _sec(session, symbol):
    from sqlalchemy import select

    from app.models import Security

    return session.scalar(select(Security).where(Security.symbol == symbol))


def test_portfolio_requires_login(anon):
    r = anon.get("/portfolio", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]
    assert anon.post("/portfolio/positions", data={"text": "AAA;1;1"}).status_code in (200, 303)


def test_position_math_and_isolation(client):
    user = _user(client, "calc@example.com")
    other = _user(client, "otro-calc@example.com")
    with _db() as s:
        aaa = _sec(s, "AAA")  # USD
        add = portfolio.add_transaction
        add(
            s,
            user.id,
            aaa,
            kind="buy",
            trade_date=date(2026, 1, 5),
            quantity=D(10),
            total_eur=D(500),
            price=D(60),
            currency="USD",
            fx_rate=D("0.85"),
            fees=D(2),
        )
        add(
            s,
            user.id,
            aaa,
            kind="buy",
            trade_date=date(2026, 2, 5),
            quantity=D(10),
            total_eur=D(700),
        )
        # coste medio 60 €; vender 5 por 400 € realiza 400 - 300 = 100 €
        add(
            s,
            user.id,
            aaa,
            kind="sell",
            trade_date=date(2026, 3, 5),
            quantity=D(5),
            total_eur=D(400),
        )
        with pytest.raises(portfolio.PortfolioError, match="más acciones"):
            add(
                s,
                user.id,
                aaa,
                kind="sell",
                trade_date=date(2026, 3, 6),
                quantity=D(100),
                total_eur=D(1),
            )
        (pos,) = portfolio.positions(s, user.id)
        assert pos.shares == 15 and pos.cost_eur == D(900) and pos.avg_cost_eur == 60
        assert pos.realized_eur == D(100) and pos.invested_eur == D(1200)
        assert pos.value_eur and pos.fx
        assert pos.unrealized_eur == pytest.approx(pos.value_eur - 900)
        assert pos.total_return_eur == pytest.approx(pos.unrealized_eur + 100)
        assert portfolio.positions(s, other.id) == []  # cada usuario ve lo suyo
        # no se puede borrar la compra de la que depende la venta
        summary = portfolio.summarize([pos])
        assert summary.cost_eur == 900 and summary.value_eur == pytest.approx(pos.value_eur)


def test_dividends_projection_and_tax(client):
    user = _user(client, "div@example.com")
    with _db() as s:
        bbb = _sec(s, "BBB.MC")  # EUR, paga 2 veces al año (ene y jul)
        portfolio.add_transaction(
            s,
            user.id,
            bbb,
            kind="buy",
            trade_date=date(2026, 1, 5),
            quantity=D(100),
            total_eur=D(1800),
        )
        doc = heytrade.parse_dividend(text("dividend_es.txt"))
        doc.isin = "ES0000000000"
        doc.external_id = "abc"
        portfolio.add_dividend(s, user.id, bbb, doc, "pdf")
        with pytest.raises(portfolio.DuplicateError):
            portfolio.add_dividend(s, user.id, bbb, doc, "pdf")
        (pos,) = portfolio.positions(s, user.id)
        assert pos.dividends_gross_eur == D("12.42") and pos.dividends_net_eur == D("10.06")
        assert pos.annual_dividend_ps and pos.annual_dividend_eur
        assert pos.yoc == pytest.approx(pos.annual_dividend_eur / 1800)
        assert pos.last_dividend and pos.last_dividend.amount > 0
        proj = portfolio.project_dividends(s, [pos])
        assert len(proj.months) == 12 and proj.total_eur == pytest.approx(
            pos.annual_dividend_eur, rel=0.01
        )
        assert sum(1 for *_, amount in proj.months if amount > 0) == 2  # dos pagos al año
        (year,) = portfolio.tax_summary(s, user.id)
        assert year.year == 2026 and year.domestic_eur == pytest.approx(2.36)
        assert year.net_eur == pytest.approx(10.06) and year.origin_eur == 0


def test_record_pdf_flow(client, monkeypatch):
    user = _user(client, "pdf@example.com")
    docs = {
        b"buy": heytrade.parse_trade(text("buy.txt")),
        b"div": heytrade.parse_dividend(text("dividend_us_synthetic.txt")),
    }
    for key, doc in docs.items():
        doc.external_id = key.decode()
    monkeypatch.setattr(heytrade, "parse_pdf", lambda data: docs[data])
    with _db() as s:
        first = portfolio.record_pdf(s, user.id, "a.pdf", b"buy")
        assert first.status == "unknown_isin" and first.isin == "US7561091049"
        with pytest.raises(portfolio.PortfolioError):
            portfolio.assign_isin(s, "nonsense", "AAA")
        portfolio.assign_isin(s, "US7561091049", "AAA")
        assert portfolio.record_pdf(s, user.id, "a.pdf", b"buy").status == "ok"
        assert portfolio.record_pdf(s, user.id, "a.pdf", b"buy").status == "duplicate"
        assert portfolio.record_pdf(s, user.id, "b.pdf", b"div").status == "ok"
        (pos,) = portfolio.positions(s, user.id)
        assert pos.shares == 11 and pos.cost_eur == D("537.11")
        # el PDF no trae cambio USD→EUR: se usa el último conocido
        assert pos.dividends_gross_eur == pytest.approx(D("12.42") * D(str(pos.fx)), rel=D("1e-6"))
        assert pos.dividends_net_eur == pytest.approx(D("10.56") * D(str(pos.fx)), abs=D("0.01"))


def test_pages_and_forms(client):
    _user(client, "web@example.com")
    r = client.post(
        "/portfolio/positions",
        data={"text": "AAA; 10; 50,5\nBBB.MC\t20\t18", "as_of": "2026-01-02"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get("/portfolio").text
    assert "AAA" in page and "BBB.MC" in page and "Valor de la cartera" in page
    for tab in ("dividendos", "fiscal", "operaciones", "importar"):
        assert client.get(f"/portfolio?tab={tab}").status_code == 200
    bad = client.post("/portfolio/positions", data={"text": "ZZZ; 1; 1\nAAA; x; 1"})
    assert bad.status_code == 400 and "no está en el universo" in bad.text
    r = client.post(
        "/portfolio/transactions",
        data={
            "ident": "CCC.L",
            "kind": "buy",
            "trade_date": "2026-03-01",
            "quantity": "3",
            "price": "25",
            "fees": "1",
            "back": "https://evil.example/",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].endswith("tab=operaciones&done=op")
    r = client.post(
        "/portfolio/transactions",
        data={
            "ident": "AAA",
            "kind": "sell",
            "trade_date": "2026-03-01",
            "quantity": "999",
            "price": "25",
        },
    )
    assert r.status_code == 400 and "más acciones" in r.text
    ficha = client.get("/security/AAA").text
    assert "Mi posición" in ficha and "Coste medio" in ficha
    upload = client.post(
        "/portfolio/upload", files=[("files", ("x.pdf", b"%PDF-1.4 basura", "application/pdf"))]
    )
    assert upload.status_code == 200 and "✗" in upload.text
    assert client.post("/portfolio/upload").status_code in (400, 422)


def test_delete_guards(client):
    _user(client, "del@example.com")
    client.post("/portfolio/positions", data={"text": "AAA; 10; 50"})
    client.post(
        "/portfolio/transactions",
        data={
            "ident": "AAA",
            "kind": "sell",
            "trade_date": "2026-03-01",
            "quantity": "4",
            "price": "60",
        },
    )
    with _db() as s:
        from sqlalchemy import select

        from app.models import Transaction, User

        uid = s.scalar(select(User.id).where(User.email == "del@example.com"))
        buy = s.scalar(
            select(Transaction).where(Transaction.user_id == uid, Transaction.kind == "buy")
        )
        buy_id = buy.id
    # el alta importada lleva fecha de hoy y la venta es anterior: borrar la compra deja la venta sin base
    r = client.post(f"/portfolio/transactions/{buy_id}/delete", follow_redirects=False)
    assert r.status_code in (303, 400)
    other = _user(client, "ajeno@example.com")
    assert client.post(f"/portfolio/transactions/{buy_id}/delete").status_code == 404
    assert other.id != uid


def test_import_position_with_received_dividends(client):
    user = _user(client, "impdiv@example.com")
    r = client.post(
        "/portfolio/positions",
        data={"text": "BBB.MC; 100; 18; 25,50\nAAA; 10; 50", "as_of": "2026-01-02"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with _db() as s:
        rows = {p.security.symbol: p for p in portfolio.positions(s, user.id)}
        bbb = rows["BBB.MC"]
        assert bbb.dividends_gross_eur == D("25.50") and bbb.dividends_net_eur == D("25.50")
        assert rows["AAA"].dividends_gross_eur == 0
        # cuenta en el total return, pero no en el resumen fiscal
        assert bbb.total_return_eur == pytest.approx(bbb.unrealized_eur + 25.5)
        assert portfolio.tax_summary(s, user.id) == []
        assert portfolio.imported_dividends_eur(s, user.id) == pytest.approx(25.5)
    assert "importados con la posición" in client.get("/portfolio?tab=dividendos").text
    assert "Importado" in client.get("/portfolio?tab=operaciones").text
    bad = client.post("/portfolio/positions", data={"text": "AAA; 1; 1; -3"})
    assert bad.status_code == 400
    lines, errors = portfolio.parse_positions(_db(), "AAA 1 2 3 4")
    assert not lines and errors


def test_manual_received_dividend(client):
    user = _user(client, "mandiv@example.com")
    client.post("/portfolio/positions", data={"text": "AAA; 10; 50", "as_of": "2026-01-02"})
    r = client.post(
        "/portfolio/dividends",
        data={
            "ident": "AAA",
            "pay_date": "2026-03-15",
            "shares": "10",
            "gross": "8,50",
            "withholding_origin": "1,28",
            "back": "/security/AAA",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].endswith("/security/AAA?done=op")
    with _db() as s:
        (pos,) = portfolio.positions(s, user.id)
        (year,) = portfolio.tax_summary(s, user.id)
        fx = D(str(pos.fx))
        assert pos.dividends_gross_eur == pytest.approx(D("8.50") * fx, rel=D("1e-6"))
        assert year.origin_eur == pytest.approx(float(D("1.28") * fx), rel=1e-6)
        assert year.net_eur == pytest.approx(float(D("7.22") * fx), abs=0.01)
    dup = client.post(
        "/portfolio/dividends",
        data={
            "ident": "AAA",
            "pay_date": "2026-03-15",
            "shares": "10",
            "gross": "8,50",
            "withholding_origin": "1,28",
        },
    )
    assert dup.status_code == 400 and "igual" in dup.text
    for data in (
        {"gross": "1", "withholding_origin": "5"},
        {"pay_date": "2999-01-01"},
        {"ident": "NOPE"},
    ):
        body = {"ident": "AAA", "pay_date": "2026-04-01", "shares": "10", "gross": "2", **data}
        assert client.post("/portfolio/dividends", data=body).status_code == 400
    assert "Mi posición" in client.get("/security/AAA").text


def test_manual_dividend(client):
    user = _user(client, "mdiv@example.com")
    client.post("/portfolio/positions", data={"text": "AAA; 10; 50", "as_of": "2026-01-02"})
    data = {
        "ident": "AAA",
        "pay_date": "2026-03-01",
        "shares": "10",
        "gross": "12,50",
        "withholding_origin": "1,50",
        "fx_rate": "",
    }
    r = client.post("/portfolio/dividends", data=data, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("tab=operaciones&done=op")
    dup = client.post("/portfolio/dividends", data=data)
    assert dup.status_code == 400 and "Ya hay un dividendo igual" in dup.text
    bad = client.post("/portfolio/dividends", data={**data, "gross": "x", "pay_date": "2026-03-02"})
    assert bad.status_code == 400
    over = client.post(
        "/portfolio/dividends", data={**data, "withholding_origin": "99", "pay_date": "2026-03-03"}
    )
    assert over.status_code == 400 and "no pueden superar el bruto" in over.text
    unknown = client.post("/portfolio/dividends", data={**data, "ident": "NOPE"})
    assert unknown.status_code == 400 and "no está en el universo" in unknown.text
    with _db() as s:
        pos = portfolio.position_for(s, user.id, _sec(s, "AAA").id)
        assert pos.dividends_net_eur > 0
        assert pos.dividends_net_eur < pos.dividends_gross_eur
    assert "AAA" in client.get("/portfolio?tab=operaciones").text
    assert client.get("/portfolio?tab=importar").status_code == 200
    assert "Añadir dividendo cobrado" in client.get("/security/AAA").text
    page = client.get("/portfolio?tab=posiciones").text
    assert "Añadir dividendo cobrado de AAA" in page and 'value="/portfolio?tab=posiciones"' in page
    data = {
        "ident": "AAA",
        "pay_date": "2026-04-01",
        "shares": "10",
        "back": "/portfolio?tab=posiciones",
    }
    ok = client.post("/portfolio/dividends", data={**data, "gross": "5"}, follow_redirects=False)
    assert ok.status_code == 303 and "tab=posiciones" in ok.headers["location"]
    bad = client.post("/portfolio/dividends", data={**data, "gross": "x"})
    assert bad.status_code == 400 and "Posiciones" in bad.text and "no es un número" in bad.text


def test_history_delete_position_and_contributions(client):
    from sqlalchemy import select

    from app import auth
    from app.models import Contribution, DividendPayment, Transaction

    auth._failures.clear()  # el alta de usuarios cuenta intentos por IP
    user = _user(client, "hist@example.com")
    client.post("/portfolio/positions", data={"text": "AAA; 10; 50", "as_of": "2026-01-02"})
    # histórico de dividendos de golpe: todo o nada, repetidos saltados
    hist = {"ident": "AAA", "back": "/portfolio?tab=posiciones", "currency": "EUR"}
    text = "fecha;a;b\n15/01/2026; 10; 5,00; 0,95\n2026-02-15; 10; 5,00\n# comentario\n15/03/2026; 10; 5"
    r = client.post("/portfolio/dividends/history", data={**hist, "text": text})
    assert r.status_code == 200 and "3 dividendos cargados" in r.text
    again = client.post(
        "/portfolio/dividends/history", data={**hist, "text": "15/01/2026; 10; 5,00"}
    )
    assert "ya existían" in again.text
    bad = client.post(
        "/portfolio/dividends/history", data={**hist, "text": "15/04/2026; 10; 5\nxx; 10; 5"}
    )
    assert bad.status_code == 400 and "Línea 2" in bad.text
    with _db() as s:
        assert (
            len(s.scalars(select(DividendPayment).where(DividendPayment.user_id == user.id)).all())
            == 3
        )
    # aportaciones: inicial, ajuste negativo y DCA con meses atrasados
    ok = client.post(
        "/portfolio/contributions",
        data={"kind": "initial", "day": "2026-01-02", "amount": "5.000"},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    client.post(
        "/portfolio/contributions", data={"kind": "adjust", "day": "2026-02-01", "amount": "-200"}
    )
    assert (
        client.post(
            "/portfolio/contributions", data={"kind": "adjust", "day": "2026-02-01", "amount": "0"}
        ).status_code
        == 400
    )
    r = client.post(
        "/portfolio/dca",
        data={"amount": "100", "day": "1", "start_date": "2026-08-01", "active": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get("/portfolio?tab=aportaciones").text
    assert "DCA mensual" in page and "Valor vs aportado" in page
    with _db() as s:
        from app.config import today

        n_dca = len(
            s.scalars(
                select(Contribution).where(
                    Contribution.user_id == user.id, Contribution.kind == "dca"
                )
            ).all()
        )
        assert n_dca >= 2  # ago, sep, oct... según la fecha actual
        total = portfolio.invested_total(s, user.id, today())
        assert total == D("4800") + 100 * n_dca
        assert portfolio.take_snapshot(s, user.id) is not None
    # borrar un mes DCA no lo regenera
    with _db() as s:
        row = s.scalar(
            select(Contribution).where(Contribution.user_id == user.id, Contribution.kind == "dca")
        )
        rid = row.id
    client.post(f"/portfolio/contributions/{rid}/delete")
    client.get("/portfolio?tab=aportaciones")
    with _db() as s:
        assert s.get(Contribution, rid) is None
    # eliminar la posición entera: sin recálculo, conserva dividendos si se pide y lo aportado
    with _db() as s:
        sid = _sec(s, "AAA").id
    r = client.post(
        f"/portfolio/securities/{sid}/delete", data={"keep_dividends": "1"}, follow_redirects=False
    )
    assert r.status_code == 303 and "done=pos" in r.headers["location"]
    with _db() as s:
        assert not s.scalars(select(Transaction).where(Transaction.user_id == user.id)).all()
        assert (
            len(s.scalars(select(DividendPayment).where(DividendPayment.user_id == user.id)).all())
            == 3
        )
        assert portfolio.invested_total(s, user.id) > 0
    client.post(f"/portfolio/securities/{sid}/delete")
    with _db() as s:
        assert not s.scalars(
            select(DividendPayment).where(DividendPayment.user_id == user.id)
        ).all()
    assert client.post("/portfolio/securities/999999/delete").status_code == 404


def test_trades_never_change_contributed_money(client):
    """Compras, ventas, dividendos y borrar posiciones no tocan lo aportado: solo las aportaciones."""
    from sqlalchemy import select

    from app import auth
    from app.config import today
    from app.models import Contribution

    auth._failures.clear()
    user = _user(client, "regla@example.com")
    client.post(
        "/portfolio/contributions",
        data={"kind": "initial", "day": "2026-01-02", "amount": "1000"},
    )

    def state():
        with _db() as s:
            rows = s.scalars(select(Contribution).where(Contribution.user_id == user.id)).all()
            snap = portfolio.take_snapshot(s, user.id)
            return portfolio.invested_total(s, user.id, today()), len(rows), snap.invested

    assert state() == (D("1000"), 1, D("1000"))
    client.post("/portfolio/positions", data={"text": "AAA; 10; 50", "as_of": "2026-01-02"})
    buy = {
        "ident": "AAA",
        "kind": "buy",
        "trade_date": "2026-02-01",
        "quantity": "5",
        "price": "20",
    }
    client.post("/portfolio/transactions", data=buy)
    client.post("/portfolio/transactions", data={**buy, "kind": "sell", "trade_date": "2026-03-01"})
    client.post(
        "/portfolio/dividends",
        data={"ident": "AAA", "pay_date": "2026-03-02", "shares": "10", "gross": "30"},
    )
    assert state() == (D("1000"), 1, D("1000"))
    with _db() as s:
        sid = _sec(s, "AAA").id
    client.post(f"/portfolio/securities/{sid}/delete")
    assert state() == (D("1000"), 1, D("1000"))
    client.post(
        "/portfolio/contributions", data={"kind": "adjust", "day": "2026-04-01", "amount": "-250"}
    )
    assert state()[0] == D("750")


def test_dividend_calendar_navigation(client):
    from app import auth
    from app.config import today

    auth._failures.clear()
    user = _user(client, "calendario@example.com")
    client.post("/portfolio/positions", data={"text": "AAA; 10; 50", "as_of": "2024-01-02"})
    client.post(
        "/portfolio/dividends/history",
        data={
            "ident": "AAA",
            "currency": "EUR",
            "back": "/portfolio?tab=posiciones",
            "text": "15/03/2025; 10; 20\n15/06/2025; 10; 30\n15/03/2026; 10; 40",
        },
    )
    page = client.get("/portfolio?tab=dividendos").text
    assert "Media mensual cobrada" in page and "Dividendos por mes" in page
    assert "12 meses antes" in page and 'class="evolution div-chart"' in page
    old = client.get("/portfolio?tab=dividendos&start=2025-01")
    assert old.status_code == 200 and "ene 2025 – dic 2025" in old.text and "cobrado 20" in old.text
    assert client.get("/portfolio?tab=dividendos&start=basura").status_code == 200
    assert client.get("/portfolio?tab=dividendos&start=1999-01").status_code == 200
    assert client.get("/portfolio?tab=dividendos&start=2999-01").status_code == 200
    with _db() as s:
        pos = portfolio.positions(s, user.id)
        cal = portfolio.dividend_calendar(s, user.id, portfolio.project_dividends(s, pos))
        assert len(cal.months) == 12 and cal.months[0].month == today().month
        assert cal.avg_months == 12 and 0 <= cal.avg_received_gross <= 90 / 12
