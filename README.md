# Stock Screener

Aplicación web ligera para inversión por dividendos en acciones de EE. UU., Canadá y Europa (el
universo comprable en HeyTrade).

1. **Screener (nivel 1):** los valores del universo separados por sector, con las métricas de un
   inversor en dividendos: yield actual, estimada y media a 5 años, PER y PER estimado, payout sobre
   beneficios y sobre FCF, crecimiento del dividendo a 5 y 10 años, años sin recorte, regla Chowder,
   rentabilidad total estimada, precio justo y precio de compra.
2. **Seguimiento (nivel 2):** los valores que marcas con ☆. Muestra el detalle de valoración, el
   semáforo de compra, el rango de 52 semanas, la distancia a la media de 200 días y gráficos de
   precio, yield histórica y dividendo por año. Cada valor admite su propio margen de seguridad,
   rentabilidad objetivo y notas.
3. **Cartera:** pendiente.

El cálculo de los precios justos y de compra está documentado en
[`docs/valoracion.md`](docs/valoracion.md).

## Stack

| Capa | Tecnología |
|---|---|
| Backend | Python 3.11+, FastAPI, Uvicorn |
| Base de datos | Postgres existente, esquema propio `stockscreener` (SQLAlchemy 2 + Alembic) |
| Datos de mercado | yfinance, detrás de la interfaz `app/providers/base.py` (sustituible) |
| Tareas periódicas | APScheduler dentro del mismo proceso (sin Redis ni Celery) |
| Frontend | Jinja2 + HTMX + Pico.css + Chart.js, servidos desde `app/static/vendor` (sin CDN ni paso de compilación) |

Divisa base: **EUR**. Los datos de mercado se guardan en la divisa del valor (las acciones de
Londres se pasan de peniques a libras).

## Universo

HeyTrade no publica su catálogo en un formato descargable. El universo se forma con:

- Los componentes de S&P 500, Nasdaq-100, S&P/TSX 60 e IBEX 35 (desde Wikipedia).
- El STOXX Europe 600, desde la cartera del ETF iShares EXSA. Cubre Madrid, París, Fráncfort,
  Milán, Ámsterdam, Londres, Zúrich, los mercados nórdicos, Lisboa, Bruselas, Viena y Dublín.
  Varsovia se omite porque HeyTrade no opera allí.
- Valores añadidos a mano o desde CSV, para lo que falte.

Cada valor usa su ticker de Yahoo: `AAPL`, `ENB.TO`, `SAN.MC`, `SAP.DE`, `ULVR.L`…

## Puesta en marcha

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .
cp .env.example .env        # edita DATABASE_URL y SECRET_KEY
alembic upgrade head        # crea el esquema "stockscreener" y sus tablas

python -m app.cli universe              # descarga los índices
python -m app.cli add SAN.MC ENB.TO     # (opcional) valores sueltos
python -m app.cli refresh bootstrap     # carga inicial completa (ver abajo)

uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

La **carga inicial** descarga la ficha de cada valor (1 petición por valor, con
`REQUEST_DELAY` segundos entre peticiones), 10 años de histórico en bloques de `BATCH_SIZE`
valores y las cuentas anuales (1 petición por valor). Con unos 1.500 valores tarda alrededor de una
hora. Se puede probar antes con unos pocos: `python -m app.cli refresh bootstrap --symbols SAN.MC AAPL`.

Las migraciones solo tocan el esquema `DB_SCHEMA`. Las demás tablas de la base de datos no se ven
afectadas.

### Tareas programadas

Se ejecutan dentro del proceso web; por eso se usa **un solo worker**. Horario de Madrid
(`TIMEZONE`):

| Tarea | Cuándo | Qué hace |
|---|---|---|
| Cotizaciones | Lunes a viernes, cada 20 min de 9 a 22 h | Último precio de todos los valores (descarga en bloque) |
| Nocturna | Lunes a viernes, 23:15 | Tipos de cambio, fichas y fundamentales, histórico y dividendos recientes, recálculo de la valoración |
| Semanal | Sábado, 10:00 | Cuentas anuales y recálculo de la valoración |

La yield actual, el PER, la rentabilidad total estimada y el semáforo se calculan en cada consulta
con la última cotización, así que siguen al precio durante el día.

Cualquier tarea se puede lanzar a mano:
`python -m app.cli refresh quotes|fx|profiles|history|financials|valuations`.

### Seguridad

La aplicación todavía **no tiene autenticación**. Hasta que la tenga, no la expongas a internet:
escucha solo en `127.0.0.1` y accede por un túnel SSH, o pon autenticación básica en el proxy
(nginx/Caddy).

## Desarrollo

```bash
pip install -e '.[dev]'
pytest                                   # tests unitarios
TEST_DATABASE_URL=postgresql+psycopg://…/bd_desechable pytest   # + integración contra Postgres
ruff check . && ruff format --check .
```

Los tests de integración usan un proveedor de datos sintético (`tests/fake_provider.py`), así que
no necesitan conexión a Yahoo. **Borran y recrean el esquema en la base de datos indicada**: usa una
base de datos desechable.
