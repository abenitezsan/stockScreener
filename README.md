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

HeyTrade no publica su catálogo en un formato descargable. `python -m app.cli universe` forma el
universo con los componentes de los índices principales de sus mercados, sacados de Wikipedia:

| Región | Índices |
|---|---|
| EE. UU. y Canadá | S&P 500, S&P/TSX 60 |
| España | IBEX 35 |
| Resto de Europa | DAX, CAC 40, FTSE 100, FTSE 250, AEX, BEL 20, SMI, FTSE MIB, PSI, ATX, ISEQ 20, OMX Stockholm 30, OMX Copenhagen 25, OMX Helsinki 25, OBX |

Si una fuente falla, las demás se cargan igual, y el error indica qué tablas encontró en la
página. Los valores que falten se añaden a mano (`add`) o desde CSV (`import-csv`).

Cada valor usa su ticker de Yahoo: `AAPL`, `ENB.TO`, `SAN.MC`, `SAP.DE`, `ULVR.L`…

Para ampliar Europa con el STOXX Europe 600 completo (unos 600 valores, incluidas medianas
empresas): iShares bloquea las descargas desde servidores, así que bájate el CSV de posiciones
("Detailed Holdings and Analytics") del ETF iShares STOXX Europe 600 (EXSA) desde el navegador y
cárgalo:

```bash
docker compose cp EXSA_holdings.csv stockscreener:/tmp/EXSA_holdings.csv
docker compose exec stockscreener python -m app.cli universe --source STOXX600 --file /tmp/EXSA_holdings.csv
```

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

En el VPS, un proxy de entrada independiente ([`deploy/edge/`](deploy/edge)) es el único
contenedor que publica el puerto 80. Reparte `/tabbito/` y `/stockscreener/` entre las apps, cada una en su propio proyecto de Docker Compose. Los
pasos de la migración están en **[deploy/README.md](deploy/README.md)**.

La imagen corre como usuario sin privilegios, tiene healthcheck en `/health` y aplica
`alembic upgrade head` al arrancar (se desactiva con `RUN_MIGRATIONS=false`).

### Base de datos y copias de seguridad

La base de datos es el fichero SQLite `data/stockscreener.db`, en una carpeta del host junto al
`docker-compose.yml` (montada en `/data` dentro del contenedor). No depende del contenedor ni de
volúmenes de Docker: se conserva al actualizar la imagen, al recrear el contenedor y con
`docker compose down -v`.

Con SQLite en modo WAL no basta con copiar el fichero mientras la app está en marcha: los
últimos cambios pueden estar aún en `stockscreener.db-wal`. Para hacer una copia consistente
en caliente:

```bash
docker compose exec stockscreener python -m app.cli backup /data/backup-$(date +%F).db
# queda en ./data/backup-AAAA-MM-DD.db en el host
```

Para restaurar una copia:
1. `docker compose stop`.
2. Sustituye `data/stockscreener.db` por la copia y borra `data/stockscreener.db-wal` y
   `data/stockscreener.db-shm` si existen.
3. `docker compose start`.

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

La aplicación no tiene login propio, y el proxy tampoco pide contraseña: en el VPS,
`/stockscreener/` es accesible para cualquiera que conozca la URL. Solo muestra datos de mercado
y tu lista de seguimiento; si más adelante quieres restringirlo, ver las notas de
[deploy/README.md](deploy/README.md).

## Desarrollo

```bash
pip install -e '.[dev]'
pytest
ruff check . && ruff format --check .
```

Los tests de integración usan un fichero SQLite temporal y un proveedor de datos sintético
(`tests/fake_provider.py`), así que no necesitan conexión a Yahoo.
