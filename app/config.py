from datetime import UTC, date, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///data/stockscreener.db"
    base_currency: str = "EUR"
    secret_key: str = "dev-insecure"
    timezone: str = "Europe/Madrid"
    # Prefijo público cuando la app va detrás de un proxy en una subruta (p. ej. /stockscreener).
    # El proxy quita el prefijo antes de reenviar; la app solo lo usa para generar enlaces.
    root_path: str = ""

    # Logs: siempre a consola; además a fichero (con rotación) si se define LOG_DIR
    log_dir: str | None = None
    log_level: str = "INFO"
    log_max_mb: int = 10
    log_backups: int = 14

    # Valoración (ver docs/valoracion.md)
    valuation_window_years: int = 5
    margin_of_safety: float = 0.10
    target_total_return: float = 0.05
    gordon_discount_rate: float = 0.08
    growth_cap: float = 0.05

    # Descarga de datos
    # Caché de yfinance (cookies de sesión con Yahoo y zonas horarias). Sin ella, yfinance se
    # vuelve a autenticar en cada petición. En Docker: /data/.cache (persiste entre reinicios).
    cache_dir: str | None = None
    request_delay: float = 1.0  # segundos entre peticiones individuales a Yahoo
    batch_size: int = 50  # símbolos por descarga masiva
    scheduler_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()


def today() -> date:
    return datetime.now(ZoneInfo(get_settings().timezone)).date()


def utcnow() -> datetime:
    return datetime.now(UTC)
