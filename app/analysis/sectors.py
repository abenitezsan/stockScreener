"""Clasificación de valores por región y por grupo de sector (reglas de sostenibilidad)."""

CA_SUFFIXES = (".TO", ".V", ".NE", ".CN")

GENERAL = "general"
UTILITIES = "utilities"
REIT = "reit"
FINANCIALS = "financials"
CYCLICAL = "cyclical"

# Grupos en los que se aplica el modelo de Gordon (dividendos estables)
STABLE_GROUPS = {UTILITIES, REIT}
STABLE_SECTORS = {"Consumer Defensive"}


def region_for(symbol: str) -> str:
    if "." not in symbol or symbol.endswith(".US"):
        return "US"
    if symbol.upper().endswith(CA_SUFFIXES):
        return "CA"
    return "EU"


def sector_group(sector: str | None, industry: str | None) -> str:
    industry = industry or ""
    if sector == "Utilities" or industry.startswith("Telecom"):
        return UTILITIES
    if sector == "Real Estate" and industry.startswith("REIT"):
        return REIT
    if sector == "Financial Services":
        return FINANCIALS
    if sector in ("Energy", "Basic Materials"):
        return CYCLICAL
    return GENERAL


def is_stable(group: str | None, sector: str | None) -> bool:
    return group in STABLE_GROUPS or sector in STABLE_SECTORS
