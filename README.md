# Stock Screener

Aplicación web ligera para inversión por dividendos en acciones de EE. UU., Canadá y Europa (el
universo comprable en HeyTrade).

1. **Screener (nivel 1):** los valores del universo separados por sector, con las métricas de un
   inversor en dividendos: yield actual, estimada y media a 5 años, PER y PER estimado, payout sobre
   beneficios y sobre FCF, crecimiento del dividendo a 5 y 10 años, años sin recorte, regla Chowder,
   rentabilidad total estimada, precio justo y precio de compra. En el móvil cada valor es una
   tarjeta con bandera, icono de sector, yield, precio, crecimiento a 5 años, precio justo y
   semáforo; en el ordenador, una tabla con esas columnas primero y el resto a continuación.
2. **Seguimiento (nivel 2):** los valores que marcas con ☆. Muestra el detalle de valoración, el
   semáforo de compra, el rango de 52 semanas, la distancia a la media de 200 días y gráficos de
   precio, yield histórica y dividendo por año. Cada valor admite su propio margen de seguridad,
   rentabilidad objetivo y notas.
3. **Cartera (privada):** posiciones valoradas en EUR, dividendos cobrados y proyectados, y resumen
   fiscal. Ver [Cartera](#cartera).

## Cartera

Menú **Cartera** (solo con sesión). Todo se calcula a partir de tus operaciones, con **coste medio
ponderado en EUR** (lo que pagaste de verdad, con el cambio y las comisiones del bróker). El valor
usa la última cotización y el último tipo de cambio (USD, GBP y demás se pasan a EUR).

- **Posiciones:** valor de la cartera, coste, revalorización, dividendos cobrados (bruto), total
  return (revalorización + ventas realizadas + dividendos brutos, sobre lo invertido), dividendo
  anual estimado, yield actual y *yield on cost* (dividendo anual a cambio actual ÷ coste). Por
  valor: acciones, coste medio, P&L, dividendo anual y último dividendo (fecha ex e importe por
  acción) con ▲/▼ frente al anterior. Reparto por sector, país y divisa. Yahoo no da la fecha de
  anuncio del dividendo, por eso se muestra la fecha ex-dividendo.
- **Dividendos:** proyección bruta de los próximos 12 meses (se repite el calendario de pagos de los
  últimos 12 con el dividendo estimado actual) y lo cobrado por año.
- **Fiscal:** por año de pago, bruto, retención en origen, retención en destino, gastos y neto
  (en EUR), y por país del valor. Informativo: no sustituye al certificado del bróker.
- **Operaciones:** compras, ventas y dividendos guardados, con borrado.
- **Añadir e importar:**
  - **PDFs de HeyTrade** (subida manual, varios a la vez): «Confirmación de operación» y
    «Confirmación del abono». Se leen con `app/heytrade.py` (plantillas fijas, campos por
    etiqueta) y los repetidos se ignoran (hash del PDF y comparación por contenido). Si el ISIN no
    corresponde a ningún valor, se pide el ticker de Yahoo una vez (si el valor no está en el
    universo, se da de alta y se descargan sus datos en segundo plano).
  - **Importar posiciones:** `ticker o ISIN; acciones; coste medio en €; dividendos cobrados en €`
    (el último dato es opcional), una por línea, como una compra a la fecha indicada (no repitas
    luego operaciones anteriores a esa fecha). Los dividendos ya cobrados cuentan en el total
    return, pero no en el resumen fiscal (no se conoce su año ni sus retenciones).
  - **Compra, venta o dividendo cobrado manuales** (con bruto y retenciones en origen y destino),
    también desde la ficha del valor (bloque **Mi posición**).

Los dividendos **no se añaden solos**: entran por PDF de HeyTrade o a mano. La proyección es solo una
previsión y no genera cobros.

Plantillas reconocidas: compra y dividendo nacional (con ejemplos reales). La **venta** y el
**dividendo extranjero** (retención en origen) se han supuesto con las mismas etiquetas y no están
verificados con un PDF real. Los tests usan el texto de los PDFs sin datos personales
(`tests/fixtures/heytrade/`).

## Buzón: PDFs de HeyTrade por email

La app puede leer sola, cada 15 minutos, un buzón IMAP al que reenvías los correos de HeyTrade
(compras, ventas y abonos de dividendos): se guardan con la fecha y los importes del PDF, sin subirlos
a mano. Se activa solo si defines `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD` y
`IMAP_ALLOWED_SENDERS` en el `.env` (ver `.env.example`).

1. **Buzón dedicado** (p. ej. GMX gratuito, con IMAP activado en *Ajustes → POP3 e IMAP*).
2. **Reenvío desde tu Gmail:** *Ajustes → Reenvío* (añadir y confirmar la dirección) y un filtro
   `from:(noreply@heytrade.com) has:attachment` → «Reenviar a». También sirve reenviar a mano.
3. **`IMAP_ALLOWED_SENDERS`:** el remitente de HeyTrade y, si reenvías a mano, tu propio Gmail.
   Un correo solo se acepta si su remitente está en la lista **y** el proveedor del buzón lo
   autentica: la cabecera `Authentication-Results` de `IMAP_AUTHSERV` (`gmx.net` por defecto) debe
   dar `dmarc=pass` (o `dkim=pass` alineado) para el dominio del remitente. Los PDFs no van
   firmados, así que sin esto cualquiera que conociera la dirección podría colar uno falso.
4. **Qué pasa con cada correo:** se leen sus PDFs, se guardan (los repetidos se ignoran) y el correo
   queda marcado como leído. Si falla algo transitorio (p. ej. la base de datos), queda sin leer y se
   reintenta. Si el ISIN no corresponde a ningún valor, el PDF espera en *Cartera → Añadir e
   importar* a que indiques su ticker y entonces se procesa solo.
5. **Las operaciones se asignan** a la cuenta `IMAP_TARGET_USER` (por defecto, `SUPERADMIN_EMAIL`).

Prueba manual en el VPS: `docker compose exec stockscreener python -m app.cli mailbox-check`.
El panel *Cartera → Añadir e importar → Buzón* muestra la última revisión (en memoria: se reinicia
con la app) y las incidencias; el detalle está en `app.log`.

## Cuentas de usuario

- El **screener y la ficha de cada valor son públicos**. El **seguimiento** y la **cartera** son
  privados: sin sesión redirigen a la pantalla de entrada. Al pulsar ☆ sin sesión sale un aviso
  para registrarse.
- Alta con **solo email y contraseña** (mínimo 8 caracteres), sin verificación del email y sin
  recuperación de contraseña: si se pierde, hay que crear otra cuenta. Al registrarse se queda con
  la sesión iniciada. Las contraseñas se guardan con scrypt y la sesión dura 30 días (cookie
  `HttpOnly`; el token se guarda en la base de datos solo como hash, así que «Salir» la invalida).
- Cada usuario tiene su propio seguimiento (con su margen de seguridad, rentabilidad objetivo y
  notas) y sus **filtros guardados**: con sesión, el botón «Guardar filtros» (dentro de «Filtros»)
  guarda los filtros actuales con un nombre y el selector «Mis filtros» los aplica. Guardar con un
  nombre existente lo actualiza. No se guarda el texto de búsqueda.
- Si la base de datos ya tenía valores en seguimiento de antes de las cuentas, los adopta el primer
  usuario que se registre.
- Hay un límite de intentos de entrada y de altas por IP (en memoria; se reinicia con la app).

### Superadministrador

La cuenta de superadmin es la que tiene el email indicado en la variable de entorno
`SUPERADMIN_EMAIL` (en `.env`). Solo ella ve la entrada **Usuarios** del menú (`/admin/users`),
donde puede ver los usuarios con su uso (valores en seguimiento, filtros guardados, sesiones
activas), **crear** cuentas, **cambiar la contraseña** de un usuario (cierra sus sesiones),
**cerrar sus sesiones** y **borrarlo** (con su seguimiento y filtros). No puede borrar su propia
cuenta. Para el resto de usuarios esa ruta no existe (404).

Como el registro es libre y no verifica el email, ese email **está reservado**: no se puede
registrar desde la web (quien lo registrara primero sería superadmin). La cuenta se crea una sola
vez desde la consola, que pide la contraseña:

```bash
docker compose exec stockscreener python -m app.cli set-password tu@email.com
```

El mismo comando sirve para **recuperar el acceso** de cualquier cuenta (cambia su contraseña y
cierra sus sesiones), por ejemplo si el superadmin olvida la suya. Si cambias `SUPERADMIN_EMAIL`,
el nuevo email pasa a ser superadmin al reiniciar (el anterior deja de serlo). Al arrancar, el log
indica si el superadmin configurado ya tiene cuenta.

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
| Resto de Europa | DAX, CAC 40, FTSE 100, FTSE 250, AEX, BEL 20, SMI, FTSE MIB, PSI, ISEQ 20, OMX Stockholm 30, OMX Copenhagen 25, OMX Helsinki 25, OBX |

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
