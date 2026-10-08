"""Buzón IMAP: verificación del remitente, lectura de adjuntos y flujo completo (IMAP simulado)."""

from email.message import EmailMessage
from pathlib import Path

import pytest

from app import heytrade, mailbox, portfolio
from app.config import get_settings
from tests.test_portfolio import _db, _user

FIXTURES = Path(__file__).parent / "fixtures" / "heytrade"
ME = "yo@gmail.com"
HEYTRADE = "no-reply@heytrade.example"

# Cabecera tal como la añade GMX a un reenvío manual desde Gmail (datos anonimizados)
AUTH_GMAIL = (
    "gmx.net; dkim=pass header.i=@gmail.com header.s=20251104; spf=pass "
    "smtp.mailfrom=yo@gmail.com; dmarc=pass header.from=gmail.com policy.dmarc=none; "
    "iprev=pass policy.iprev=74.125.82.47"
)
AUTH_HEYTRADE = (
    "gmx.net; dkim=pass header.d=heytrade.example header.s=ses; spf=pass "
    "smtp.mailfrom=bounce@amazonses.com; dmarc=pass header.from=heytrade.example"
)


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("IMAP_ALLOWED_SENDERS", f"{ME}, {HEYTRADE.upper()}")
    monkeypatch.setenv("IMAP_AUTHSERV", "gmx.net")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def email_bytes(
    sender=ME, auth=AUTH_GMAIL, pdfs=(b"%PDF-1.4 uno",), extra_headers=(), subject="Fwd: HeyTrade"
):
    msg = EmailMessage()
    if auth:
        msg["Authentication-Results"] = auth
    for name, value in extra_headers:
        msg[name] = value
    msg["From"] = f"Alguien <{sender}>"
    msg["To"] = "buzon@gmx.com"
    msg["Subject"] = subject
    msg.set_content("---------- Forwarded message ---------")
    for i, data in enumerate(pdfs):
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=f"doc{i}.pdf")
    return msg.as_bytes()


class FakeClient:
    def __init__(self, messages):
        self.messages = dict(enumerate(messages, 1))
        self.seen: set[int] = set()

    def unseen(self, limit):
        for uid, raw in list(self.messages.items()):
            if uid not in self.seen:
                yield str(uid).encode(), raw

    def mark_seen(self, uid):
        self.seen.add(int(uid))


# --- Verificación del remitente ----------------------------------------------------------


def check(raw):
    from email import message_from_bytes, policy

    return mailbox.check_sender(message_from_bytes(raw, policy=policy.default))


def test_trusted_senders():
    assert check(email_bytes()) == ME  # reenvío manual: DMARC de gmail.com
    assert check(email_bytes(sender=HEYTRADE, auth=AUTH_HEYTRADE)) == HEYTRADE  # reenvío por filtro
    only_dkim = "gmx.net; dkim=pass header.i=@mail.heytrade.example"  # subdominio alineado
    assert check(email_bytes(sender=HEYTRADE, auth=only_dkim)) == HEYTRADE


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({"sender": "intruso@evil.example"}, "no permitido"),
        ({"auth": None}, "sin Authentication-Results"),
        ({"auth": "otro.servidor; dmarc=pass header.from=gmail.com"}, "sin Authentication-Results"),
        (
            {"auth": "gmx.net; dkim=fail header.i=@gmail.com; dmarc=fail header.from=gmail.com"},
            "no pasa",
        ),
        (
            {"auth": "gmx.net; dkim=pass header.i=@otro.com; dmarc=pass header.from=otro.com"},
            "no pasa",
        ),  # firmado, pero por un dominio que no es el del remitente
    ],
)
def test_untrusted_senders(kwargs, reason):
    with pytest.raises(mailbox.Rejected, match=reason):
        check(email_bytes(**kwargs))


def test_injected_authentication_results_is_ignored():
    # Un atacante añade su propia cabecera «buena»; la de GMX (la de arriba) dice fail
    raw = email_bytes(
        auth="gmx.net; dkim=fail header.i=@gmail.com; dmarc=fail header.from=gmail.com",
        extra_headers=[("Authentication-Results", AUTH_GMAIL)],
    )
    with pytest.raises(mailbox.Rejected):
        check(raw)


def test_extract_pdfs_ignores_other_attachments():
    from email import message_from_bytes, policy

    msg = EmailMessage()
    msg.set_content("hola")
    msg.add_attachment(b"%PDF-1.4 ok", maintype="application", subtype="pdf", filename="a.pdf")
    msg.add_attachment(b"no soy pdf", maintype="application", subtype="pdf", filename="b.pdf")
    msg.add_attachment(b"zip", maintype="application", subtype="zip", filename="c.zip")
    inner = EmailMessage()
    inner.add_attachment(
        b"%PDF-1.4 dentro", maintype="application", subtype="pdf", filename="d.pdf"
    )
    msg.add_attachment(inner)  # correo reenviado como adjunto
    parsed = message_from_bytes(msg.as_bytes(), policy=policy.default)
    assert [n for n, _ in mailbox.extract_pdfs(parsed)] == ["a.pdf", "d.pdf"]


# --- Flujo completo -----------------------------------------------------------------------


def test_poll_flow(client, monkeypatch):
    user = _user(client, "buzon@example.com")
    docs = {
        b"%PDF-1.4 compra": heytrade.parse_trade((FIXTURES / "buy.txt").read_text()),
        b"%PDF-1.4 dividendo": heytrade.parse_dividend(
            (FIXTURES / "dividend_us_synthetic.txt").read_text()
        ),
    }
    for key, doc in docs.items():
        doc.external_id = key.decode()
        doc.isin = "ES0000000001"  # propio de este test: la base de datos es compartida

    def fake_parse(data):
        if data not in docs:
            raise heytrade.ParseError("Plantilla desconocida")
        return docs[data]

    monkeypatch.setattr(heytrade, "parse_pdf", fake_parse)
    spoof = email_bytes(sender="intruso@evil.example", pdfs=(b"%PDF-1.4 compra",))
    good = email_bytes(pdfs=(b"%PDF-1.4 compra", b"%PDF-1.4 dividendo"))
    junk = email_bytes(pdfs=(b"%PDF-1.4 raro",), subject="Otro")
    client_imap = FakeClient([spoof, good, junk])

    with _db() as s:
        status = mailbox.poll(s, user.id, client_imap)
        # Todo queda leído: el descartado, el bueno (valor desconocido) y el ilegible
        assert client_imap.seen == {1, 2, 3} and status.messages == 3
        assert portfolio.positions(s, user.id) == []  # nada guardado aún: falta el ticker
        assert portfolio.pending_summary(s, user.id) == [("ES0000000001", "Realty Income Corp.", 2)]
        text = " ".join(status.log)
        assert "evil.example" in text and "Plantilla desconocida" in text
        page = client.get("/portfolio?tab=importar").text
        assert "Documentos pendientes de ticker" in page and "ES0000000001" in page
        # Al asignar el ticker, los pendientes se procesan solos (compra y dividendo, mismo ISIN)
        security = portfolio.assign_isin(s, "ES0000000001", "BBB.MC")
        results = portfolio.process_pending(s, user.id, "ES0000000001")
        assert [r.status for r in results] == ["ok", "ok"]
        assert portfolio.pending_summary(s, user.id) == []
        (pos,) = portfolio.positions(s, user.id)
        assert pos.security.id == security.id and pos.shares == 11
        assert pos.dividends_gross_eur > 0  # el dividendo del mismo valor también se guardó
        gross = pos.dividends_gross_eur
        # Reprocesar el mismo correo no duplica nada
        mailbox.poll(s, user.id, FakeClient([good]))
        (pos,) = portfolio.positions(s, user.id)
        assert pos.shares == 11 and pos.dividends_gross_eur == gross


def test_transient_error_leaves_message_unseen(client, monkeypatch):
    user = _user(client, "buzon2@example.com")

    def boom(*args, **kwargs):
        raise RuntimeError("base de datos bloqueada")

    monkeypatch.setattr(portfolio, "record_pdf", boom)
    fake = FakeClient([email_bytes()])
    with _db() as s:
        mailbox.poll(s, user.id, fake)
    assert fake.seen == set()  # se reintentará en la siguiente vuelta


def test_configured_and_status_panel(client, monkeypatch):
    assert not mailbox.configured()  # sin host, usuario ni contraseña
    monkeypatch.setenv("IMAP_HOST", "imap.gmx.com")
    monkeypatch.setenv("IMAP_USER", "buzon@gmx.com")
    monkeypatch.setenv("IMAP_PASSWORD", "x")
    monkeypatch.setenv("IMAP_TARGET_USER", "nadie@example.com")
    get_settings.cache_clear()
    assert mailbox.configured()
    _user(client, "panel@example.com")
    page = client.get("/portfolio?tab=importar").text
    assert "Buzón" in page
    assert mailbox.run_poll(_db()).startswith("No existe la cuenta destino")
