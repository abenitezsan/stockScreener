"""Universo de valores: componentes de índices que cubren los mercados de HeyTrade.

Las listas se descargan de Wikipedia y de la cartera del ETF iShares STOXX Europe 600, y los
tickers se traducen al formato de Yahoo. Lo que falte se añade a mano (`manual`) o desde CSV.
"""

import csv
import io
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import pandas as pd
from curl_cffi import requests as cffi_requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.sectors import region_for
from app.models import Security

log = logging.getLogger(__name__)

MANUAL = "manual"

# Palabra clave de la columna "Exchange" de iShares -> sufijo de Yahoo. El orden importa
# ("nordic" genérico es Estocolmo, pero Copenhague y Helsinki también dicen "Nordic").
EXCHANGE_SUFFIXES = [
    ("madrid", ".MC"),
    ("xetra", ".DE"),
    ("frankfurt", ".DE"),
    ("deutsche b", ".DE"),
    ("paris", ".PA"),
    ("amsterdam", ".AS"),
    ("brussels", ".BR"),
    ("lisbon", ".LS"),
    ("dublin", ".IR"),
    ("irish", ".IR"),
    ("italiana", ".MI"),
    ("milan", ".MI"),
    ("london", ".L"),
    ("swiss", ".SW"),
    ("zurich", ".SW"),
    ("oslo", ".OL"),
    ("copenhagen", ".CO"),
    ("helsinki", ".HE"),
    ("stockholm", ".ST"),
    ("nordic", ".ST"),
    ("wiener", ".VI"),
    ("vienna", ".VI"),
]


def fetch(url: str) -> str:
    """Descarga identificándose como un Chrome real (huella TLS incluida). Algunas webs, como la
    de iShares, rechazan con 403 las peticiones que no parecen de un navegador."""
    response = cffi_requests.get(url, impersonate="chrome", timeout=30)
    response.raise_for_status()
    return response.content.decode("utf-8-sig", errors="replace")


# --- Traducción de tickers -------------------------------------------------------


def us_symbol(ticker: str) -> str:
    return ticker.strip().upper().replace(".", "-")  # BRK.B -> BRK-B


def with_suffix(ticker: str, suffix: str) -> str:
    """ACS -> ACS.MC; NOVO B -> NOVO-B.CO; RR. -> RR.L; BT.A -> BT-A.L; ya con sufijo, igual."""
    ticker = ticker.strip().upper()
    if ticker.endswith(suffix.upper()):
        return ticker
    ticker = re.sub(r"[.\s/]+", "-", ticker.rstrip(".")).strip("-")
    return ticker + suffix


def exchange_suffix(exchange: str) -> str | None:
    name = exchange.lower()
    return next((suffix for key, suffix in EXCHANGE_SUFFIXES if key in name), None)


# --- Fuentes ---------------------------------------------------------------------


def _column_name(column) -> str:
    """'Ticker[12]' -> 'ticker'; 'Ticker symbol' -> 'ticker symbol'."""
    return re.sub(r"\[.*?\]", "", str(column)).strip().lower()


def _wiki_symbols(
    html: str, columns: tuple[str, ...], translate: Callable[[str], str]
) -> list[str]:
    """Busca la primera tabla con alguna de las columnas indicadas y traduce sus tickers.

    Tolera notas al pie y variantes ("Ticker[a]", "Ticker symbol"). Si no encuentra nada, el
    error lista las columnas de las tablas de la página para poder ajustar la fuente.
    """
    wanted = {c.lower() for c in columns}
    seen = []
    for table in pd.read_html(io.StringIO(html)):
        if isinstance(table.columns, pd.MultiIndex):
            table.columns = [c[-1] for c in table.columns]
        if len(table) < 10:
            continue
        seen.append([str(c) for c in table.columns])
        column = next(
            (
                c
                for c in table.columns
                if _column_name(c) in wanted or (_column_name(c).split() or [""])[0] in wanted
            ),
            None,
        )
        if column is not None:
            return [translate(str(t)) for t in table[column].dropna() if str(t).strip()]
    raise ValueError(f"No se encontró ninguna tabla con las columnas {columns}. Tablas: {seen}")


def parse_ishares_holdings(text: str) -> list[str]:
    """CSV de posiciones de iShares: unas líneas de cabecera y después la tabla."""
    lines = text.splitlines()
    start = next(
        i
        for i, line in enumerate(lines)
        if line.startswith(("Ticker", '"Ticker', "Emittententicker"))
    )
    symbols = []
    for row in csv.DictReader(lines[start:]):
        ticker = row.get("Ticker") or row.get("Emittententicker") or ""
        asset_class = row.get("Asset Class") or row.get("Anlageklasse") or "Equity"
        exchange = row.get("Exchange") or row.get("Börse") or ""
        if not ticker or asset_class not in ("Equity", "Aktien"):
            continue
        suffix = exchange_suffix(exchange)
        if suffix is None:
            log.info("Bolsa no cubierta, se omite: %s (%s)", ticker, exchange)
            continue
        symbols.append(with_suffix(ticker, suffix))
    return symbols


@dataclass(frozen=True)
class Source:
    url: str
    parse: Callable[[str], list[str]]


SOURCES: dict[str, Source] = {
    "SP500": Source(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        lambda html: _wiki_symbols(html, ("Symbol",), us_symbol),
    ),
    "NASDAQ100": Source(
        "https://en.wikipedia.org/wiki/Nasdaq-100",
        lambda html: _wiki_symbols(html, ("Ticker", "Symbol"), us_symbol),
    ),
    "TSX60": Source(
        "https://en.wikipedia.org/wiki/S%26P/TSX_60",
        lambda html: _wiki_symbols(html, ("Symbol", "Ticker"), lambda t: with_suffix(t, ".TO")),
    ),
    "IBEX35": Source(
        "https://en.wikipedia.org/wiki/IBEX_35",
        lambda html: _wiki_symbols(html, ("Ticker", "Symbol"), lambda t: with_suffix(t, ".MC")),
    ),
    "STOXX600": Source(
        "https://www.ishares.com/uk/individual/en/products/251931/"
        "ishares-stoxx-europe-600-ucits-etf-de-fund/1506575576011.ajax"
        "?fileType=csv&fileName=EXSA_holdings&dataType=fund",
        parse_ishares_holdings,
    ),
}


# --- Persistencia ----------------------------------------------------------------


def upsert_universe(session: Session, universe: str, symbols: Iterable[str]) -> int:
    """Añade la etiqueta `universe` a los símbolos dados y se la quita a los que ya no están.

    Un valor que se queda sin ninguna etiqueta se desactiva (no se borra: puede tener historia).
    Son unos pocos miles de filas: se hace en Python, sin trucos de SQL.
    """
    wanted = {s.strip().upper() for s in symbols if s.strip()}
    existing = {sec.symbol: sec for sec in session.scalars(select(Security))}
    for symbol in sorted(wanted - existing.keys()):
        session.add(Security(symbol=symbol, region=region_for(symbol), universes=[universe]))
    for symbol, sec in existing.items():
        tags = list(sec.universes or [])
        if symbol in wanted:
            if universe not in tags:
                sec.universes = [*tags, universe]
            sec.active = True
        elif universe in tags and universe != MANUAL:
            sec.universes = [t for t in tags if t != universe]
            if not sec.universes:
                sec.active = False
    session.commit()
    return len(wanted)


def sync_sources(
    session: Session, names: Iterable[str] | None = None, file: str | None = None
) -> dict[str, int | str]:
    """Descarga cada fuente y actualiza el universo. Un fallo en una fuente no para las demás.

    Con `file`, el contenido de la (única) fuente se lee de ese fichero en lugar de descargarlo:
    p. ej. el CSV de iShares bajado a mano desde el navegador.
    """
    names = list(names or SOURCES)
    if file and len(names) != 1:
        raise ValueError("--file solo se puede usar con una única fuente (--source)")
    result: dict[str, int | str] = {}
    for name in names:
        source = SOURCES[name]
        try:
            if file:
                with open(file, encoding="utf-8-sig", errors="replace") as f:
                    content = f.read()
            else:
                content = fetch(source.url)
            symbols = source.parse(content)
        except Exception as exc:
            log.exception("Error cargando %s", name)
            result[name] = f"error: {exc}"
            continue
        result[name] = upsert_universe(session, name, symbols)
    return result


def import_csv(session: Session, path: str, universe: str = MANUAL) -> int:
    """CSV con una columna `symbol` (ticker de Yahoo), o un ticker por línea."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if rows and rows[0] and rows[0][0].strip().lower() == "symbol":
        rows = rows[1:]
    return upsert_universe(session, universe, [r[0].strip().upper() for r in rows if r and r[0]])


def active_symbols(session: Session) -> list[tuple[int, str]]:
    return list(
        session.execute(
            select(Security.id, Security.symbol).where(Security.active).order_by(Security.symbol)
        )
    )
