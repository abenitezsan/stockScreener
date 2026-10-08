"""Universo de valores: componentes de índices que cubren los mercados de HeyTrade.

Las listas se descargan de Wikipedia y de la cartera del ETF iShares STOXX Europe 600, y los
tickers se traducen al formato de Yahoo. Lo que falte se añade a mano (`manual`) o desde CSV.
"""

import csv
import io
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import pandas as pd
from curl_cffi import requests as cffi_requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.sectors import region_for
from app.config import get_settings
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
]


def fetch(url: str) -> str:
    """Descarga identificándose como un Chrome real (huella TLS incluida). Algunas webs, como la
    de iShares, rechazan con 403 las peticiones que no parecen de un navegador."""
    response = cffi_requests.get(url, impersonate="chrome", timeout=30)
    response.raise_for_status()
    return response.content.decode("utf-8-sig", errors="replace")


# --- Traducción de tickers -------------------------------------------------------


def clean_ticker(ticker: str) -> str:
    """Quita notas al pie y prefijos de bolsa de Wikipedia: 'LSE: AAL[3]' -> 'AAL'."""
    ticker = re.sub(r"\[.*?\]", "", str(ticker))
    return ticker.rsplit(":", 1)[-1].strip().upper()


def us_symbol(ticker: str) -> str:
    return clean_ticker(ticker).replace(".", "-")  # BRK.B -> BRK-B


def with_suffix(ticker: str, suffix: str) -> str:
    """ACS -> ACS.MC; NOVO B -> NOVO-B.CO; RR. -> RR.L; BT.A -> BT-A.L; ya con sufijo, igual."""
    ticker = clean_ticker(ticker)
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


NAME_COLUMNS = (
    "Company",
    "Name",
    "Company name",
    "Constituent",
    "Constituent name",
    "Empresa",
    "Compañía",
    "Nombre",
)


def _find_column(tables: list[pd.DataFrame], columns: tuple[str, ...]):
    """Primera tabla (de al menos 10 filas) con alguna de las columnas; tolera notas al pie y
    variantes ("Ticker[a]", "Ticker symbol")."""
    wanted = {c.lower() for c in columns}
    for table in tables:
        for c in table.columns:
            name = _column_name(c)
            if name in wanted or (name.split() or [""])[0] in wanted:
                return table, c
    return None, None


def _wiki_symbols(
    html: str,
    columns: tuple[str, ...],
    translate: Callable[[str], str],
    resolve_names: Callable[[list[str]], list[str]] | None = None,
) -> list[str]:
    """Tickers de la tabla de componentes de una página de Wikipedia.

    Si la tabla no trae tickers sino solo nombres de empresa, y hay
    `resolve_names`, los nombres se traducen a tickers buscándolos en el proveedor de datos.
    Si no encuentra nada, el error lista las columnas de las tablas de la página.
    """
    tables = []
    for table in pd.read_html(io.StringIO(html)):
        if isinstance(table.columns, pd.MultiIndex):
            table.columns = [c[-1] for c in table.columns]
        if len(table) >= 10:
            tables.append(table)
    table, column = _find_column(tables, columns)
    if column is not None:
        return [translate(str(t)) for t in table[column].dropna() if str(t).strip()]
    if resolve_names is not None:
        table, column = _find_column(tables, NAME_COLUMNS)
        if column is not None:
            names = [re.sub(r"\[.*?\]", "", str(n)).strip() for n in table[column].dropna()]
            return resolve_names([n for n in names if n])
    seen = [[str(c) for c in t.columns] for t in tables]
    raise ValueError(f"No se encontró ninguna tabla con las columnas {columns}. Tablas: {seen}")


def resolve_company_names(names: list[str], suffix: str) -> list[str]:
    """Busca cada empresa en el proveedor y se queda con el ticker de la bolsa de `suffix`."""
    from app.providers import get_provider

    provider = get_provider()
    delay = get_settings().request_delay
    symbols, missing = [], []
    for name in names:
        try:
            symbol = provider.find_symbol(name, suffix)
        except Exception:  # un nombre que falla no debe tumbar el índice entero
            log.exception("Error buscando %s", name)
            symbol = None
        if symbol:
            symbols.append(symbol)
        else:
            missing.append(name)
        time.sleep(delay)
    log.info("Nombres resueltos a tickers %s: %d/%d", suffix, len(symbols), len(names))
    if missing:
        log.warning("Sin ticker %s para: %s (añádelos con `add`)", suffix, ", ".join(missing))
    return symbols


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
    # False: no se descarga con un `universe` sin --source (p. ej. la web bloquea al servidor)
    default: bool = True


# "MNEM code": códigos nemotécnicos de Euronext Dublin (ISEQ 20), iguales a los de Yahoo (.IR)
TICKER_COLUMNS = (
    "Ticker",
    "Symbol",
    "Ticker symbol",
    "Stock symbol",
    "EPIC",
    "Code",
    "MNEM",
    "Símbolo",
    "Código",
)


def wiki(url: str, suffix: str, lang: str = "en") -> Source:
    """Página de Wikipedia con una tabla de componentes; los tickers llevan `suffix` en Yahoo."""
    return Source(
        f"https://{lang}.wikipedia.org/wiki/{url}",
        lambda html: _wiki_symbols(
            html,
            TICKER_COLUMNS,
            lambda t: with_suffix(t, suffix),
            lambda names: resolve_company_names(names, suffix),
        ),
    )


SOURCES: dict[str, Source] = {
    # --- Norteamérica
    "SP500": Source(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        lambda html: _wiki_symbols(html, ("Symbol",), us_symbol),
    ),
    "TSX60": wiki("S%26P/TSX_60", ".TO"),
    # --- Europa: índices nacionales de los mercados de HeyTrade (Wikipedia)
    "IBEX35": wiki("IBEX_35", ".MC"),
    # Mediana capitalización española (Ebro, Viscofan…): la tabla solo está en la Wikipedia en español
    "IBEXMC": wiki("IBEX_Medium_Cap", ".MC", lang="es"),
    "DAX": wiki("DAX", ".DE"),
    "CAC40": wiki("CAC_40", ".PA"),
    "FTSE100": wiki("FTSE_100_Index", ".L"),
    "FTSE250": wiki("FTSE_250_Index", ".L"),
    "AEX": wiki("AEX_index", ".AS"),
    "BEL20": wiki("BEL_20", ".BR"),
    "SMI": wiki("Swiss_Market_Index", ".SW"),
    "FTSEMIB": wiki("FTSE_MIB", ".MI"),
    "PSI": wiki("PSI-20", ".LS"),
    "ISEQ20": wiki("ISEQ_20", ".IR"),
    "OMXS30": wiki("OMX_Stockholm_30", ".ST"),
    "OMXC25": wiki("OMX_Copenhagen_25", ".CO"),
    "OMXH25": wiki("OMX_Helsinki_25", ".HE"),
    "OBX": wiki("OBX_Index", ".OL"),
    # --- Europa completa (STOXX Europe 600). iShares bloquea las descargas desde servidores:
    # se carga con el CSV bajado desde el navegador (--source STOXX600 --file ...)
    "STOXX600": Source(
        "https://www.ishares.com/uk/individual/en/products/251931/"
        "ishares-stoxx-europe-600-ucits-etf-de-fund/1506575576011.ajax"
        "?fileType=csv&fileName=EXSA_holdings&dataType=fund",
        parse_ishares_holdings,
        default=False,
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
    names = list(names or [n for n, src in SOURCES.items() if src.default])
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
