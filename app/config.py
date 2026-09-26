from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://localhost/stockscreener"
    db_schema: str = "stockscreener"
    base_currency: str = "EUR"
    secret_key: str = "dev-insecure"


@lru_cache
def get_settings() -> Settings:
    return Settings()
