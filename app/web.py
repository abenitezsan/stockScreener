"""Rutas web (HTML generado en el servidor + HTMX)."""

from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import Undefined
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import QueryParams

from app import auth, portfolio, screener
from app.analysis import sectors, valuation
from app.analysis.dividends import TtmIndex, analyze
from app.config import get_settings, today, utcnow
from app.db import get_session
from app.models import (
    DividendEvent,
    FinancialsAnnual,
    PriceHistory,
    SavedFilter,
    Security,
    User,
    UserSession,
    WatchlistItem,
)

DbSession = Annotated[Session, Depends(get_session)]


# --- Usuario actual ------------------------------------------------------------------


class LoginRequired(Exception):
    """La página es privada: se redirige al login y luego se vuelve a `next`."""

    def __init__(self, next_url: str, reason: str):
        self.next_url, self.reason = next_url, reason


def current_user(request: Request, session: DbSession) -> User | None:
    user = auth.user_for_token(session, request.cookies.get(auth.COOKIE))
    request.state.user = user  # lo leen las plantillas (nav, botón de seguir…)
    return user


CurrentUser = Annotated[User | None, Depends(current_user)]


def _here(request: Request) -> str:
    """Ruta actual sin el prefijo público (el proxy ya lo quita) y con la query."""
    query = request.url.query
    return request.url.path + (f"?{query}" if query else "")


def page_user(reason: str):
    """Usuario para una página privada: sin sesión, redirige al login con un aviso."""

    def dependency(request: Request, user: CurrentUser) -> User:
        if user is None:
            raise LoginRequired(_here(request), reason)
        return user

    return Annotated[User, Depends(dependency)]


def _api_user(user: CurrentUser) -> User:
    if user is None:
        raise HTTPException(401, "Necesitas iniciar sesión")
    return user


def _admin_user(request: Request, user: CurrentUser) -> User:
    if user is None:
        raise LoginRequired(_here(request), "admin")
    if not auth.is_superadmin(user):
        raise HTTPException(404)  # a los demás usuarios no se les revela que existe
    return user


ApiUser = Annotated[User, Depends(_api_user)]
AdminUser = Annotated[User, Depends(_admin_user)]
WatchlistUser = page_user("watchlist")
PortfolioUser = page_user("portfolio")
WatchUser = page_user("watch")

router = APIRouter(dependencies=[Depends(current_user)])


def _template_context(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    return {
        "user": user,
        "is_admin": auth.is_superadmin(user),
        "here": quote(_here(request), safe=""),
    }


templates = Jinja2Templates(
    directory=Path(__file__).parent / "templates", context_processors=[_template_context]
)

FLAG_LABELS = {
    "payout_alto": "Payout sobre beneficios alto",
    "payout_fcf_alto": "Payout sobre FCF alto",
    "payout_ocf_alto": "Payout sobre flujo operativo alto",
    "fcf_negativo": "FCF negativo",
    "bpa_negativo": "BPA negativo",
    "sin_datos_payout": "Faltan datos de payout",
    "sin_dividendo": "No paga dividendo",
    "pocos_datos": "Menos de 3 años de dividendos",
    "recorte_reciente": "Recorte en los últimos 5 años",
    "yield_anomala": "Yield > 1,5× su media (¿trampa?)",
    "dividendos_reescalados": "Dividendos corregidos ×100 (unidades)",
}
GROUP_LABELS = {
    "general": "General",
    "utilities": "Utilities y telecos",
    "reit": "REIT",
    "financials": "Financieras",
    "cyclical": "Cíclicas",
}
SIGNAL_LABELS = {
    "green": ("●", "Compra"),
    "yellow": ("◐", "Vigilar"),
    "red": ("○", "Cara"),
    "none": ("–", "Sin datos"),
}
HARD_FLAGS = valuation.HARD_FLAGS
# Sector de Yahoo -> (icono del sprite _icons.html, nombre en castellano)
SECTORS = {
    "Basic Materials": ("pickaxe", "Materiales"),
    "Communication Services": ("radio-tower", "Comunicaciones"),
    "Consumer Cyclical": ("shopping-bag", "Consumo cíclico"),
    "Consumer Defensive": ("shopping-cart", "Consumo defensivo"),
    "Energy": ("flame", "Energía"),
    "Financial Services": ("landmark", "Financieras"),
    "Healthcare": ("heart-pulse", "Salud"),
    "Industrials": ("factory", "Industria"),
    "Real Estate": ("building", "Inmobiliario"),
    "Technology": ("cpu", "Tecnología"),
    "Utilities": ("zap", "Utilities"),
}
# País de Yahoo -> (código ISO, nombre en castellano). Los no listados se muestran en texto.
COUNTRIES = {
    "United States": ("US", "EE. UU."),
    "Canada": ("CA", "Canadá"),
    "United Kingdom": ("GB", "Reino Unido"),
    "Ireland": ("IE", "Irlanda"),
    "Germany": ("DE", "Alemania"),
    "France": ("FR", "Francia"),
    "Spain": ("ES", "España"),
    "Portugal": ("PT", "Portugal"),
    "Italy": ("IT", "Italia"),
    "Netherlands": ("NL", "Países Bajos"),
    "Belgium": ("BE", "Bélgica"),
    "Luxembourg": ("LU", "Luxemburgo"),
    "Switzerland": ("CH", "Suiza"),
    "Austria": ("AT", "Austria"),
    "Sweden": ("SE", "Suecia"),
    "Norway": ("NO", "Noruega"),
    "Denmark": ("DK", "Dinamarca"),
    "Finland": ("FI", "Finlandia"),
    "Iceland": ("IS", "Islandia"),
    "Poland": ("PL", "Polonia"),
    "Czech Republic": ("CZ", "Chequia"),
    "Greece": ("GR", "Grecia"),
    "Cyprus": ("CY", "Chipre"),
    "Malta": ("MT", "Malta"),
    "Hungary": ("HU", "Hungría"),
    "Jersey": ("JE", "Jersey"),
    "Guernsey": ("GG", "Guernsey"),
    "Isle of Man": ("IM", "Isla de Man"),
    "Bermuda": ("BM", "Bermudas"),
    "Cayman Islands": ("KY", "Islas Caimán"),
    "Puerto Rico": ("PR", "Puerto Rico"),
    "Mexico": ("MX", "México"),
    "Brazil": ("BR", "Brasil"),
    "Chile": ("CL", "Chile"),
    "Israel": ("IL", "Israel"),
    "Australia": ("AU", "Australia"),
    "Japan": ("JP", "Japón"),
    "China": ("CN", "China"),
    "Hong Kong": ("HK", "Hong Kong"),
    "Singapore": ("SG", "Singapur"),
    "Taiwan": ("TW", "Taiwán"),
    "South Korea": ("KR", "Corea del Sur"),
    "India": ("IN", "India"),
    "South Africa": ("ZA", "Sudáfrica"),
    "Monaco": ("MC", "Mónaco"),
    "Uruguay": ("UY", "Uruguay"),
    "Argentina": ("AR", "Argentina"),
    "Peru": ("PE", "Perú"),
    "Colombia": ("CO", "Colombia"),
    "Panama": ("PA", "Panamá"),
}


def _fmt_number(value, digits: int = 2) -> str:
    if value is None:
        return "–"
    value = float(value)
    return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")


def _d_norm(value) -> str:
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def fmt(value, kind: str = "money") -> str:
    # `x if cond` sin else en Jinja da Undefined: se muestra como dato ausente
    if value is None or isinstance(value, Undefined):
        return "–"
    match kind:
        case "pct":
            return _fmt_number(float(value) * 100, 1) + " %"
        case "spct":  # con signo
            return f"{float(value) * 100:+,.1f}".replace(",", " ").replace(".", ",") + " %"
        case "seur":
            return f"{float(value):+,.2f}".replace(",", " ").replace(".", ",") + " €"
        case "qty":  # acciones: sin ceros sobrantes
            text = f"{_d_norm(value)}"
            whole, _, frac = text.partition(".")
            return f"{int(whole):,}".replace(",", " ") + (f",{frac}" if frac else "")
        case "x":
            return _fmt_number(value, 1)
        case "int":
            return str(int(value))
        case "cap":
            if value >= 1e9:
                return _fmt_number(value / 1e9, 1) + " mil M€"
            return _fmt_number(value / 1e6, 0) + " M€"
        case "eur":
            return _fmt_number(value, 2) + " €"
        case "text":
            return str(value)
        case _:
            return _fmt_number(value, 2)


def country_flag(country: str | None) -> tuple[str, str]:
    """(bandera emoji, nombre) de un país de Yahoo; sin código conocido, texto sin bandera."""
    if not country:
        return "", "País desconocido"
    code, name = COUNTRIES.get(country, ("", country))
    # Bandera = par de "regional indicator symbols" a partir del código ISO
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code), name


def gain_class(value) -> str:
    """Clase CSS para colorear ganancias (verde) y pérdidas (rojo)."""
    if value is None or isinstance(value, Undefined) or float(value) == 0:
        return ""
    return "pos" if float(value) > 0 else "neg"


templates.env.filters["fmt"] = fmt
templates.env.filters["gain"] = gain_class
templates.env.filters["country_flag"] = country_flag
templates.env.filters["flag_labels"] = lambda flags: "; ".join(
    FLAG_LABELS.get(f, f) for f in flags or []
)
ROOT = get_settings().root_path.rstrip("/")


def _asset_version() -> str:
    """Cambia cuando cambia algún estático propio: obliga al navegador a descargarlo de nuevo."""
    static = Path(__file__).parent / "static"
    stamp = max((static / name).stat().st_mtime_ns for name in ("app.css", "app.js", "charts.js"))
    return format(stamp // 1_000_000_000, "x")


templates.env.globals.update(
    ROOT=ROOT,
    ASSET_V=_asset_version(),
    FLAG_LABELS=FLAG_LABELS,
    HARD_FLAGS=HARD_FLAGS,
    SECTORS=SECTORS,
    GROUP_LABELS=GROUP_LABELS,
    SIGNAL_LABELS=SIGNAL_LABELS,
    COLUMNS=screener.COLUMNS,
    REGIONS=screener.REGIONS,
)


# --- Filtros desde la URL ------------------------------------------------------------


def _float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def filters_from_params(p: QueryParams) -> screener.Filters:
    f = screener.Filters()
    if "submitted" not in p:  # primera carga: valores por defecto
        f.sector = p.get("sector", "")
        return f
    f.sector = p.get("sector", "")
    f.regions = [r for r in p.getlist("region") if r in screener.REGIONS]
    f.q = p.get("q", "").strip()
    f.yield_avg_min = _float(p.get("yield_avg_min"))
    f.yield_avg_max = _float(p.get("yield_avg_max"))
    f.yield_min = _float(p.get("yield_min"))
    f.yield_max = _float(p.get("yield_max"))
    f.payout_max = _float(p.get("payout_max"))
    f.dgr_min = _float(p.get("dgr_min"))
    years = _float(p.get("years_no_cut_min"))
    f.years_no_cut_min = int(years) if years is not None else None
    f.cap_min_bn = _float(p.get("cap_min_bn"))
    f.dividend_only = p.get("dividend_only") == "1"
    f.quality_only = p.get("quality_only") == "1"
    f.signal = p.get("signal", "") if p.get("signal") in SIGNAL_LABELS else ""
    f.sort = p.get("sort", "yield_ttm") if p.get("sort") in screener.COLUMNS else "yield_ttm"
    f.desc = p.get("desc", "1") == "1"
    return f


def parse_filters(request: Request) -> screener.Filters:
    return filters_from_params(request.query_params)


def _num(value: float) -> str:
    return f"{value:g}"


def filters_to_params(f: screener.Filters) -> list[list[str]]:
    """Parámetros de URL equivalentes a unos filtros (sin la búsqueda de texto), en el mismo
    formato que lee `filters_from_params`. Lo que no aparece queda en blanco al aplicarlos."""
    params = [["sector", f.sector]] if f.sector else []
    params += [["region", r] for r in f.regions]
    for name in (
        "yield_avg_min",
        "yield_avg_max",
        "yield_min",
        "yield_max",
        "payout_max",
        "dgr_min",
        "years_no_cut_min",
        "cap_min_bn",
    ):
        if (value := getattr(f, name)) is not None:
            params.append([name, _num(value)])
    if f.signal:
        params.append(["signal", f.signal])
    if f.dividend_only:
        params.append(["dividend_only", "1"])
    if f.quality_only:
        params.append(["quality_only", "1"])
    params += [["sort", f.sort], ["desc", "1" if f.desc else "0"]]
    return params


def saved_filter_url(saved: SavedFilter) -> str:
    query = urlencode([("submitted", "1"), *saved.params, ("f", saved.id)])
    return f"{ROOT}/screener?{query}"


MAX_SAVED_FILTERS = 30


def _screener_context(
    session: Session, f: screener.Filters, user: User | None, active_filter: int | None
) -> dict:
    saved = (
        list(
            session.scalars(
                select(SavedFilter)
                .where(SavedFilter.user_id == user.id)
                .order_by(func.lower(SavedFilter.name))
            )
        )
        if user
        else []
    )
    return {
        "filters": f,
        "saved_filters": [(sf, saved_filter_url(sf)) for sf in saved],
        "active_filter": active_filter,
        "rows": screener.screen(session, f),
        "sectors": screener.sector_counts(session, f),
        "key_columns": screener.SCREENER_KEY_COLUMNS,
        "extra_columns": [
            c
            for c in screener.SCREENER_COLUMNS
            if c not in screener.SCREENER_KEY_COLUMNS
            and c not in ("symbol", "name", "sector", "country")
        ],
        "watched": screener.watchlist_ids(session, user.id if user else None),
    }


# --- Rutas ---------------------------------------------------------------------------


@router.get("/")
def index():
    return RedirectResponse(f"{ROOT}/screener")


@router.get("/screener", response_class=HTMLResponse)
def screener_page(request: Request, session: DbSession, user: CurrentUser):
    active = request.query_params.get("f", "")
    ctx = _screener_context(
        session, parse_filters(request), user, int(active) if active.isdecimal() else None
    )
    # Con HTMX (cambio de filtros) solo se devuelve la tabla
    partial = request.headers.get("HX-Request") and not request.headers.get(
        "HX-History-Restore-Request"
    )
    template = "_screener_results.html" if partial else "screener.html"
    return templates.TemplateResponse(request, template, ctx)


@router.post("/watchlist/{security_id}/toggle", response_class=HTMLResponse)
def toggle_watch(request: Request, security_id: int, session: DbSession, user: ApiUser):
    if session.get(Security, security_id) is None:
        raise HTTPException(404)
    item = screener.watch_item(session, user.id, security_id)
    if item:
        session.delete(item)
    else:
        session.add(WatchlistItem(user_id=user.id, security_id=security_id))
    session.commit()
    return templates.TemplateResponse(
        request, "_watch_button.html", {"id": security_id, "watched": item is None}
    )


@router.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request, session: DbSession, user: WatchlistUser):
    return templates.TemplateResponse(
        request, "watchlist.html", {"rows": screener.watchlist(session, user.id)}
    )


@router.get("/security/{symbol}", response_class=HTMLResponse)
def security_page(request: Request, symbol: str, session: DbSession, user: CurrentUser):
    sec = session.scalar(select(Security).where(Security.symbol == symbol.upper()))
    if sec is None:
        raise HTTPException(404, "Valor no encontrado")
    saved_item = screener.watch_item(session, user.id if user else None, sec.id)
    row = screener.watch_row(session, saved_item or WatchlistItem(security_id=sec.id))
    closes = session.execute(
        select(PriceHistory.day, PriceHistory.close)
        .where(PriceHistory.security_id == sec.id)
        .order_by(PriceHistory.day)
    ).all()
    dividends = session.execute(
        select(DividendEvent.ex_date, DividendEvent.amount)
        .where(DividendEvent.security_id == sec.id)
        .order_by(DividendEvent.ex_date)
    ).all()
    return templates.TemplateResponse(
        request,
        "security.html",
        {
            "row": row,
            "sec": sec,
            "watched": saved_item is not None,
            "settings": get_settings(),
            "chart_data": _chart_data(row, closes, dividends),
            "hard_reasons": hard_flag_reasons(session, sec, row),
            "position": portfolio.position_for(session, user.id, sec.id) if user else None,
            "today": today(),
        },
    )


def _big(value: float | None, currency: str) -> str:
    """Importe de las cuentas anuales en millones o miles de millones."""
    if value is None:
        return "–"
    if abs(value) >= 1e9:
        return f"{_fmt_number(value / 1e9, 1)} mil M {currency}".strip()
    if abs(value) >= 1e6:
        return f"{_fmt_number(value / 1e6, 0)} M {currency}".strip()
    return f"{_fmt_number(value, 0)} {currency}".strip()


def hard_flag_reasons(
    session: Session, sec: Security, row: screener.WatchRow
) -> list[tuple[str, str]]:
    """Por qué el dividendo no pasa el filtro de sostenibilidad, con las cifras de cada motivo."""
    v, f = row.valuation, row.fundamentals
    flags = [flag for flag in (v.flags if v else []) if flag in HARD_FLAGS]
    if not flags:
        return []
    group = sec.sector_group or sectors.GENERAL
    eps_limit, fcf_limit, ocf_limit = valuation.PAYOUT_LIMITS[group]
    group_label = GROUP_LABELS.get(group, group)
    latest = session.scalar(
        select(FinancialsAnnual)
        .where(FinancialsAnnual.security_id == sec.id, FinancialsAnnual.dividends_paid.is_not(None))
        .order_by(FinancialsAnnual.period_end.desc())
        .limit(1)
    )
    ccy = sec.financial_currency or sec.currency or ""
    year = latest.period_end.year if latest else "el último ejercicio"
    paid = _big(latest.dividends_paid if latest else None, ccy)
    reasons = {
        "payout_alto": (
            f"Reparte en dividendos el {fmt(f.payout_ratio if f else None, 'pct')} del beneficio "
            f"de los últimos 12 meses, por encima del {fmt(eps_limit, 'pct')} que se admite en "
            f"{group_label.lower()}. Queda poco margen si los beneficios bajan."
        ),
        "payout_fcf_alto": (
            f"En {year} pagó {paid} en dividendos, el {fmt(v.payout_fcf, 'pct')} de su flujo de "
            f"caja libre ({_big(latest.free_cashflow if latest else None, ccy)}); el límite es "
            f"{fmt(fcf_limit, 'pct')}. La caja que genera apenas cubre el dividendo."
        ),
        "payout_ocf_alto": (
            f"En {year} pagó {paid} en dividendos, el {fmt(v.payout_ocf, 'pct')} de su flujo "
            f"operativo ({_big(latest.operating_cashflow if latest else None, ccy)}); el límite "
            f"para {group_label.lower()} es {fmt(ocf_limit, 'pct')}."
        ),
        "fcf_negativo": (
            f"En {year} su flujo de caja libre fue negativo "
            f"({_big(latest.free_cashflow if latest else None, ccy)}) y aun así pagó {paid} en "
            "dividendos: los financió con deuda o con caja acumulada."
        ),
        "bpa_negativo": (
            f"El beneficio por acción de los últimos 12 meses es negativo "
            f"({fmt(f.eps_ttm if f else None)}): la empresa está en pérdidas y el dividendo no "
            "tiene beneficios que lo respalden."
        ),
    }
    return [(FLAG_LABELS[flag], reasons[flag]) for flag in flags]


def _chart_data(row: screener.WatchRow, closes, dividends) -> dict:
    """Series para los gráficos: precio 2 años, yield 10 años y dividendo por año."""
    stats = analyze([(d, a) for d, a in dividends], today())
    index = TtmIndex(stats.regular)
    recent = closes[-105:]  # ~2 años de cierres semanales
    first_full = index.first and (index.first.toordinal() + 365)
    yields = [
        (d.isoformat(), round(index.at(d) / c * 100, 3))
        for d, c in closes
        if c > 0 and first_full and d.toordinal() >= first_full
    ]
    v = row.valuation
    return {
        "price": [(d.isoformat(), round(c, 4)) for d, c in recent],
        "fair_value": v.fair_value if v else None,
        "buy_price": row.buy_price,
        "yield": yields,
        "yield_avg": v.yield_avg_5y * 100 if v and v.yield_avg_5y else None,
        "yield_p80": v.yield_p80_5y * 100 if v and v.yield_p80_5y else None,
        "annual": [(str(y), round(a, 4)) for y, a in stats.annual.items()],
        "currency": row.security.currency or "",
    }


@router.post("/security/{symbol}/watch")
def update_watch(
    symbol: str,
    session: DbSession,
    user: WatchUser,
    notes: Annotated[str, Form()] = "",
    margin_of_safety: Annotated[str, Form()] = "",
    target_total_return: Annotated[str, Form()] = "",
):
    sec = session.scalar(select(Security).where(Security.symbol == symbol.upper()))
    if sec is None:
        raise HTTPException(404)
    item = screener.watch_item(session, user.id, sec.id) or WatchlistItem(
        user_id=user.id, security_id=sec.id
    )
    item.notes = notes.strip() or None
    mos, target = _float(margin_of_safety), _float(target_total_return)
    item.margin_of_safety = mos / 100 if mos is not None else None
    item.target_total_return = target / 100 if target is not None else None
    session.add(item)
    session.commit()
    return RedirectResponse(f"{ROOT}/security/{sec.symbol}", status_code=303)


# --- Cuenta: entrar, crear cuenta y salir -------------------------------------------

REASONS = {
    "watchlist": "Entra para ver tus valores en seguimiento.",
    "watch": "Necesitas estar registrado para añadir empresas a seguimiento.",
    "portfolio": "La cartera es privada: entra para verla.",
    "admin": "Entra con la cuenta de superadministrador.",
}


def _safe_next(value: str | None) -> str:
    """Solo rutas internas (nada de `//host` ni esquemas) y nunca las propias de la cuenta."""
    if (
        value
        and value.startswith("/")
        and not value.startswith(("//", "/login", "/register", "/logout"))
        and "\\" not in value
        and all(ord(c) >= 32 for c in value)
    ):
        return value
    return "/screener"


def _auth_page(
    request: Request, mode: str, next_url: str, email: str = "", error: str = "", reason: str = ""
):
    ctx = {
        "mode": mode,
        "next": next_url,
        "email": email,
        "error": error,
        "notice": REASONS.get(reason, ""),
        "min_password": auth.MIN_PASSWORD,
    }
    return templates.TemplateResponse(request, "auth.html", ctx, status_code=400 if error else 200)


def _login_redirect(request: Request, session: Session, user: User, next_url: str):
    response = RedirectResponse(f"{ROOT}{_safe_next(next_url)}", status_code=303)
    response.set_cookie(
        auth.COOKIE,
        auth.start_session(session, user),
        max_age=auth.SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path=ROOT or "/",
    )
    return response


def _client_key(request: Request, prefix: str, email: str = "") -> str:
    return f"{prefix}|{request.client.host if request.client else '-'}|{email}"


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user: CurrentUser, next: str = "", reason: str = ""):
    if user:
        return RedirectResponse(f"{ROOT}{_safe_next(next)}", status_code=303)
    return _auth_page(request, "login", _safe_next(next), reason=reason)


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    session: DbSession,
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "",
):
    email = auth.normalize_email(email)
    key = _client_key(request, "login", email)
    if auth.throttled(key):
        return _auth_page(
            request, "login", _safe_next(next), email, "Demasiados intentos. Espera unos minutos."
        )
    user = (
        auth.authenticate(session, email, password) if len(password) <= auth.MAX_PASSWORD else None
    )
    if user is None:
        auth.record_failure(key)
        return _auth_page(
            request, "login", _safe_next(next), email, "Email o contraseña incorrectos."
        )
    auth.clear_failures(key)
    return _login_redirect(request, session, user, next)


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, user: CurrentUser, next: str = ""):
    if user:
        return RedirectResponse(f"{ROOT}{_safe_next(next)}", status_code=303)
    return _auth_page(request, "register", _safe_next(next))


@router.post("/register", response_class=HTMLResponse)
def register(
    request: Request,
    session: DbSession,
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "",
):
    email = auth.normalize_email(email)
    key = _client_key(request, "register")
    if auth.throttled(key):
        return _auth_page(
            request,
            "register",
            _safe_next(next),
            email,
            "Demasiados intentos. Espera unos minutos.",
        )
    auth.record_failure(key)  # cuenta cada alta, también las buenas: frena el alta masiva
    error = auth.validate_credentials(email, password)
    # El email del superadmin está reservado (se crea con la CLI); se responde igual que a un duplicado
    user = (
        None
        if error or auth.is_reserved_email(email)
        else auth.create_user(session, email, password)
    )
    if not error and user is None:
        error = "Ya existe una cuenta con ese email."
    if error:
        return _auth_page(request, "register", _safe_next(next), email, error)
    return _login_redirect(request, session, user, next)  # queda con la sesión iniciada


@router.post("/logout")
def logout(request: Request, session: DbSession):
    auth.end_session(session, request.cookies.get(auth.COOKIE))
    response = RedirectResponse(f"{ROOT}/screener", status_code=303)
    response.delete_cookie(auth.COOKIE, path=ROOT or "/")
    return response


# --- Gestión de usuarios (solo superadmin) -----------------------------------------

ADMIN_DONE = {
    "created": "Usuario creado.",
    "password": "Contraseña cambiada y sesiones de ese usuario cerradas.",
    "sessions": "Sesiones cerradas.",
    "deleted": "Usuario borrado, con su seguimiento y sus filtros guardados.",
}


def _admin_users(session: Session) -> list:
    def count(model, *conditions):
        return (
            select(func.count())
            .where(model.user_id == User.id, *conditions)
            .correlate(User)
            .scalar_subquery()
        )

    return session.execute(
        select(
            User,
            count(WatchlistItem).label("watching"),
            count(SavedFilter).label("filters"),
            count(UserSession, UserSession.expires_at > utcnow()).label("sessions"),
        ).order_by(User.created_at, User.id)
    ).all()


def _admin_page(
    request: Request, session: Session, error: str = "", done: str = "", email: str = ""
):
    ctx = {
        "rows": _admin_users(session),
        "error": error,
        "notice": ADMIN_DONE.get(done, ""),
        "email": email,
        "min_password": auth.MIN_PASSWORD,
    }
    return templates.TemplateResponse(
        request, "admin_users.html", ctx, status_code=400 if error else 200
    )


def _admin_redirect(done: str) -> RedirectResponse:
    return RedirectResponse(f"{ROOT}/admin/users?done={done}", status_code=303)


def _admin_target(session: Session, user_id: int) -> User:
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(404)
    return target


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request, session: DbSession, admin: AdminUser, done: str = ""):
    return _admin_page(request, session, done=done)


@router.post("/admin/users", response_class=HTMLResponse)
def admin_create_user(
    request: Request,
    session: DbSession,
    admin: AdminUser,
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
):
    email = auth.normalize_email(email)
    error = auth.validate_credentials(email, password)
    if not error and auth.create_user(session, email, password) is None:
        error = "Ya existe una cuenta con ese email."
    if error:
        return _admin_page(request, session, error, email=email)
    return _admin_redirect("created")


@router.post("/admin/users/{user_id}/password", response_class=HTMLResponse)
def admin_set_password(
    request: Request,
    user_id: int,
    session: DbSession,
    admin: AdminUser,
    password: Annotated[str, Form()] = "",
):
    target = _admin_target(session, user_id)
    if error := auth.validate_password(password):
        return _admin_page(request, session, f"{target.email}: {error}")
    auth.set_password(session, target, password)
    return _admin_redirect("password")


@router.post("/admin/users/{user_id}/sessions")
def admin_close_sessions(user_id: int, session: DbSession, admin: AdminUser):
    auth.delete_sessions(session, _admin_target(session, user_id).id)
    session.commit()
    return _admin_redirect("sessions")


@router.post("/admin/users/{user_id}/delete", response_class=HTMLResponse)
def admin_delete_user(request: Request, user_id: int, session: DbSession, admin: AdminUser):
    target = _admin_target(session, user_id)
    if auth.is_superadmin(target):
        return _admin_page(request, session, "No puedes borrar la cuenta del superadministrador.")
    auth.delete_user(session, target)
    return _admin_redirect("deleted")


# --- Filtros guardados (solo con sesión; el screener sigue siendo público) -----------


def _filter_error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


@router.post("/screener/filters")
async def save_filter(request: Request, session: DbSession, user: ApiUser):
    form = await request.form()
    name = " ".join(str(form.get("name", "")).split())
    if not name:
        return _filter_error("Escribe un nombre para el filtro.")
    if len(name) > 40:
        return _filter_error("El nombre admite hasta 40 caracteres.")
    pairs = [
        (k, v)
        for k, v in form.multi_items()
        if isinstance(v, str) and k not in ("name", "q", "submitted", "f") and len(v) <= 100
    ]
    params = filters_to_params(filters_from_params(QueryParams([("submitted", "1"), *pairs])))
    existing = session.scalar(
        select(SavedFilter).where(
            SavedFilter.user_id == user.id, func.lower(SavedFilter.name) == name.lower()
        )
    )
    if existing is None:
        count = session.scalar(select(func.count()).where(SavedFilter.user_id == user.id))
        if count >= MAX_SAVED_FILTERS:
            return _filter_error(f"Máximo {MAX_SAVED_FILTERS} filtros guardados: borra alguno.")
        existing = SavedFilter(user_id=user.id, name=name)
        session.add(existing)
    existing.name, existing.params = name, params  # al repetir nombre se actualiza
    session.commit()
    return {"url": saved_filter_url(existing)}


@router.post("/screener/filters/{filter_id}/delete")
def delete_filter(filter_id: int, session: DbSession, user: ApiUser):
    saved = session.get(SavedFilter, filter_id)
    if saved is None or saved.user_id != user.id:
        raise HTTPException(404)
    session.delete(saved)
    session.commit()
    return {"url": f"{ROOT}/screener"}
