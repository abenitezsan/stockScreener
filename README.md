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
| Base de datos | SQLite en modo WAL, un único fichero (SQLAlchemy 2 + Alembic) |
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

## Despliegue con Docker (recomendado)

GitHub Actions (`.github/workflows/docker.yml`) ejecuta el lint, comprueba las migraciones y pasa
los tests en cada push. Si todo pasa, publica la imagen en Docker Hub para
`linux/amd64` y `linux/arm64`:

| Origen | Etiquetas |
|---|---|
| Push a la rama por defecto | `latest`, `sha-<commit>` |
| Tag `v1.2.3` | `1.2.3`, `1.2`, `sha-<commit>` |
| Pull requests y otras ramas | solo compila, no publica |

### 1. Configurar Docker Hub y GitHub (una vez)

1. En Docker Hub: *Account settings → Personal access tokens → Generate new token* con permiso
   **Read & Write**.
2. En GitHub, en el repositorio: *Settings → Secrets and variables → Actions*:
   - Secret `DOCKERHUB_USERNAME`: tu usuario de Docker Hub.
   - Secret `DOCKERHUB_TOKEN`: el token del paso anterior.
   - (Opcional) Variable `DOCKERHUB_IMAGE`, si quieres otro nombre distinto de
     `<usuario>/stockscreener`.
3. Lanza el workflow (*Actions → CI y Docker Hub → Run workflow*) o haz un push a la rama por
   defecto.

### 2. Arrancar en el VPS

En un directorio del VPS, deja el `docker-compose.yml` del repositorio y un `.env` basado en
`.env.example`. Como mínimo:

```bash
# .env
STOCKSCREENER_IMAGE=tuusuario/stockscreener:latest
```

```bash
docker compose pull && docker compose up -d        # aplica las migraciones al arrancar
docker compose exec stockscreener python -m app.cli universe
docker compose exec stockscreener python -m app.cli refresh bootstrap
docker compose logs -f
```

Para actualizar: `docker compose pull && docker compose up -d`. Para quedarte en una versión
concreta, usa una etiqueta `vX.Y.Z` en `STOCKSCREENER_IMAGE` en lugar de `latest`.

La imagen corre como usuario sin privilegios, tiene healthcheck en `/health` y aplica
`alembic upgrade head` al arrancar (se desactiva con `RUN_MIGRATIONS=false`).

### Base de datos y copias de seguridad

La base de datos es el fichero SQLite `/data/stockscreener.db`, dentro del volumen de Docker
`stockscreener-data`. El volumen se conserva al actualizar la imagen y al hacer
`docker compose down`, pero **se borra con `docker compose down -v`**.

Con SQLite en modo WAL no basta con copiar el fichero mientras la app está en marcha. Para hacer
una copia consistente y sacarla del volumen:

```bash
docker compose exec stockscreener python -m app.cli backup /data/backup.db
docker compose cp stockscreener:/data/backup.db ./stockscreener-$(date +%F).db
```

Para restaurar una copia: `docker compose stop`, copia el fichero a
`/data/stockscreener.db` (con `docker compose cp`) y vuelve a arrancar.

## Puesta en marcha sin Docker

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .
cp .env.example .env        # opcional: DATABASE_URL, SECRET_KEY…
alembic upgrade head        # crea data/stockscreener.db y sus tablas

python -m app.cli universe              # descarga los índices
python -m app.cli add SAN.MC ENB.TO     # (opcional) valores sueltos
python -m app.cli refresh bootstrap     # carga inicial completa (ver abajo)

uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

La **carga inicial** descarga la ficha de cada valor (1 petición por valor, con
`REQUEST_DELAY` segundos entre peticiones), 10 años de histórico en bloques de `BATCH_SIZE`
valores y las cuentas anuales (1 petición por valor). Con unos 1.500 valores tarda alrededor de una
hora. Se puede probar antes con unos pocos: `python -m app.cli refresh bootstrap --symbols SAN.MC AAPL`.

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
pytest
ruff check . && ruff format --check .
```

Los tests de integración usan un fichero SQLite temporal y un proveedor de datos sintético
(`tests/fake_provider.py`), así que no necesitan conexión a Yahoo.
