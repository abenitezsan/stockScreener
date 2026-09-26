# Stock Screener

Aplicación web ligera para:

1. **Screener** de acciones de EE. UU., Canadá y Europa (el universo comprable en HeyTrade).
2. **Cartera**: posiciones calculadas a partir de las operaciones de compra/venta, valoradas en EUR.
3. **Dividendos**: registro de dividendos cobrados y previstos (al estilo de DivTracker).

## Stack

| Capa | Tecnología |
|---|---|
| Backend | Python 3.11+, FastAPI, Uvicorn |
| Base de datos | Postgres existente, esquema propio `stockscreener` (SQLAlchemy 2 + Alembic) |
| Datos de mercado | yfinance, detrás de la interfaz `app/providers/base.py` (sustituible) |
| Tareas periódicas | APScheduler dentro del mismo proceso (sin Redis ni Celery) |
| Frontend | Jinja2 + HTMX + Alpine.js + Pico.css (sin paso de compilación) |

Divisa base: **EUR**. Los importes se guardan en su divisa original junto con el tipo de cambio.

## Universo

HeyTrade no publica su catálogo en un formato descargable. El universo se forma con:

- Los componentes de índices que cubren sus mercados: S&P 500, Nasdaq-100, S&P/TSX 60,
  STOXX Europe 600 (cubre Madrid, París, Fráncfort, Milán, Ámsterdam, Londres, Zúrich, nórdicos,
  Lisboa, Bruselas, Viena y Dublín) e IBEX 35 junto con el mercado continuo.
- Valores añadidos a mano (`universes = {manual}`), para lo que falte.

Cada valor usa su ticker de Yahoo: `AAPL`, `ENB.TO`, `SAN.MC`, `SAP.DE`, `ULVR.L`…

## Puesta en marcha

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env        # edita DATABASE_URL y SECRET_KEY
alembic upgrade head        # crea el esquema "stockscreener" y sus tablas
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Las migraciones solo tocan el esquema `DB_SCHEMA`. Las demás tablas de la base de datos no se ven afectadas.

## Desarrollo

```bash
pytest
ruff check . && ruff format --check .
```
