from app.providers.base import MarketDataProvider
from app.providers.yahoo import YahooProvider


def get_provider() -> MarketDataProvider:
    return YahooProvider()
