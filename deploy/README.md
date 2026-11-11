# Despliegue en el VPS: proxy de entrada + tabbito + stockscreener

El VPS solo expone el puerto 80. En lugar de que el nginx de tabbito haga también de proxy de
las demás apps, hay un **proxy de entrada independiente** (`edge`) en su propia carpeta. Es el
único contenedor que publica el puerto 80, y se conecta a la red de cada app:

```
                         ┌─ /tabbito/        ──► app:8080            (red tabbito_tabbito-net)
internet :80 ──► edge ───┤
                         └─ /stockscreener/  ──► stockscreener:8000  (red stockscreener-net)
```

Cada app sigue siendo un proyecto de Docker Compose independiente, sin puertos publicados. Se
pueden actualizar, parar o reiniciar por separado:

- Si una app está caída, solo su ruta devuelve 502. El proxy resuelve los nombres en cada
  petición, así que arranca aunque falte alguna app.
- Un `docker compose down` de una app no rompe la conexión: Docker conserva su red mientras
  el proxy esté conectado, y al volver a levantarla la reutiliza.

## Estructura en el VPS

```
/home/debian/
├── tabbito/                 # como ahora, pero sin el servicio nginx
├── stockscreener/
│   ├── docker-compose.yml   # ../docker-compose.yml de este repositorio
│   ├── .env                 # opcional (ver ../.env.example)
│   ├── data/                # stockscreener.db (SQLite) y copias de seguridad
│   └── logs/                # app.log (web y tareas programadas) y cli.log, con rotación
└── edge/                    # copia de deploy/edge/ de este repositorio
    ├── docker-compose.yml
    ├── conf.d/default.conf  # rutas de las dos apps
    └── logs/                # access.log / error.log de nginx
```

## Migración paso a paso

El corte de tabbito dura solo unos segundos, entre los pasos 4 y 5.

### 1. Arrancar stockscreener

Crea `/home/debian/stockscreener/` con el `docker-compose.yml` del repositorio y ejecuta:

```bash
cd /home/debian/stockscreener
mkdir -p data logs && sudo chown 10001:10001 data logs   # la app corre con uid 10001
docker compose pull && docker compose up -d        # crea la red stockscreener-net
```

El `chown` es necesario:
- **Sin él en `data/`**, el contenedor no arranca y `docker compose logs` indica el comando
  exacto para arreglarlo.
- **Sin él en `logs/`**, la app arranca igual pero solo escribe los logs en consola.

### 2. Preparar el proxy

```bash
mkdir -p /home/debian/edge && cp -r deploy/edge/. /home/debian/edge/    # desde un clon del repo
```

### 3. Comprobar el nombre de la red de tabbito

```bash
docker network ls | grep tabbito
```

Por defecto se espera `tabbito_tabbito-net`: el proyecto de Compose toma el nombre del
directorio del `docker-compose.prod.yml`. Si el nombre es otro, créate en `edge/` un `.env` con:

```bash
TABBITO_NETWORK=<nombre que aparezca>
```

### 4. Quitar el nginx de tabbito

En `docker-compose.prod.yml` de tabbito, **borra el servicio `nginx` completo**. No cambia nada
más: la app, la base de datos, `tabbito-net` y `ADMIN_BASE_URL` siguen igual, porque la ruta
pública sigue siendo `/tabbito/`. El `tabbito.conf` deja de usarse; sus reglas están ahora en
`edge/conf.d/default.conf`.

```bash
cd <directorio de tabbito>
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --remove-orphans
```

`--remove-orphans` elimina el contenedor de nginx que ya no está en el fichero y libera el
puerto 80. La app y la base de datos de tabbito no se reinician.

### 5. Arrancar el proxy

```bash
cd /home/debian/edge
docker compose up -d
```

Comprueba:
- `http://<IP>/tabbito/` → el admin de tabbito, igual que antes.
- `http://<IP>/stockscreener/` → el screener.

### 6. Carga inicial de datos del screener

```bash
cd /home/debian/stockscreener
docker compose exec stockscreener python -m app.cli universe
docker compose exec stockscreener python -m app.cli refresh bootstrap    # ~1 hora
```

## Operación

| Tarea | Dónde | Comando |
|---|---|---|
| Actualizar stockscreener | `stockscreener/` | `docker compose pull && docker compose up -d` |
| Logs del screener | `stockscreener/` | `tail -f logs/app.log` (web, peticiones y tareas programadas) · `logs/cli.log` (comandos) |
| Copia de seguridad del screener | `stockscreener/` | `docker compose exec stockscreener python -m app.cli backup /data/backup-$(date +%F).db` (queda en `data/`) |
| Cambiar la configuración de nginx | `edge/` | edita `conf.d/default.conf`, luego `docker compose exec nginx nginx -t && docker compose exec nginx nginx -s reload` |
| Ver peticiones | `edge/` | `tail -f logs/access.log` |

Para añadir otra app en el futuro:
1. Dale a su red un nombre fijo en su `docker-compose.yml`.
2. Añade esa red como `external` en `edge/docker-compose.yml`.
3. Añade su `location` en `conf.d/default.conf`.
4. Ejecuta `docker compose up -d` en `edge/`.

## HTTPS (Let's Encrypt)

Dominio `tabkiper.com` (registros A del dominio y de `www` apuntando al VPS). El proxy termina
TLS y sirve tabbito desde la **raíz** del dominio (`https://tabkiper.com/admin/...`,
`/site/...`, `/webchat/...`, `/api/...`), stockscreener en `https://tabkiper.com/stockscreener/`,
`www` redirige al dominio sin www, y `/tabbito/` sigue funcionando por compatibilidad. El acceso
por IP sigue siendo HTTP y con las rutas de siempre.

nginx no arranca referenciando un certificado que aún no existe, así que la primera vez se usa
`bootstrap.conf` (solo HTTP + el reto ACME) y luego la configuración final.

```bash
sudo mkdir -p /var/www/certbot /etc/letsencrypt
cd /home/debian/edge

# 1. Arrancar con la config de arranque
cp conf.d/default.conf conf.d/default.conf.final
cp bootstrap.conf conf.d/default.conf
docker compose up -d --force-recreate

# 2. Emitir el certificado (dominio y www)
docker run --rm \
  -v /etc/letsencrypt:/etc/letsencrypt -v /var/www/certbot:/var/www/certbot \
  certbot/certbot certonly --webroot -w /var/www/certbot \
  -d tabkiper.com -d www.tabkiper.com --email TU_CORREO --agree-tos --no-eff-email

# 3. Poner la config final y recargar
mv conf.d/default.conf.final conf.d/default.conf
docker compose exec nginx nginx -t && docker compose up -d --force-recreate
```

Comprueba `https://tabkiper.com/site/index.html`, `https://tabkiper.com/admin/login.html` y
`https://tabkiper.com/stockscreener/`.

**Renovación**: los certificados duran 90 días. Cron del host (`sudo crontab -e`):

```
0 4 * * * docker run --rm -v /etc/letsencrypt:/etc/letsencrypt -v /var/www/certbot:/var/www/certbot certbot/certbot renew --quiet && docker compose -f /home/debian/edge/docker-compose.yml exec -T nginx nginx -s reload
```

**En tabbito** (`.env.prod`), para webhooks y enlaces:

```
ADMIN_BASE_URL=https://tabkiper.com/admin
SPRING_PROFILES_ACTIVE=pro
TELEGRAM_WEBHOOK_BASE_URL=https://tabkiper.com
```

La ruta pública de tabbito pasa de `/tabbito/admin` a `/admin`, por eso `ADMIN_BASE_URL` ya no
lleva el prefijo (los callbacks de Redsys y PayPal derivan su origen de ese valor).

## Monitorización (mon.tabkiper.com)

El proxy publica la monitorización de tabbito (proyecto `tabbito-monitoring`, en el repo de
tabbito: `docker-compose.monitoring.yml`) en un subdominio, **todo detrás de contraseña**:

- `https://mon.tabkiper.com/vm/vmui/` — consultas sobre las métricas (VictoriaMetrics).
- `https://mon.tabkiper.com/traffic/` — informe de GoAccess sobre `logs/access.log` de este proxy.

Puesta en marcha, una sola vez:

```bash
# 1. DNS: registro A de mon.tabkiper.com hacia el VPS (en OVH).

# 2. Contraseña. El fichero no se versiona (deploy/edge/auth/*).
cd /home/debian/edge
printf 'admin:%s\n' "$(openssl passwd -apr1)" > auth/monitor.htpasswd

# 3. Aplicar la config y recargar (el bloque HTTPS de mon ya usa el certificado actual,
#    pero el navegador avisará de nombre inválido hasta el paso 4).
docker compose up -d --force-recreate

# 4. Ampliar el certificado con el subdominio (mismo certificado, un nombre más).
docker run --rm \
  -v /etc/letsencrypt:/etc/letsencrypt -v /var/www/certbot:/var/www/certbot \
  certbot/certbot certonly --webroot -w /var/www/certbot --expand \
  -d tabkiper.com -d www.tabkiper.com -d mon.tabkiper.com
docker compose exec nginx nginx -s reload
```

El informe `/traffic/` lo escribe el proyecto de monitorización en
`/home/debian/tabbito/monitoring/report` y este proxy lo monta en solo lectura; hasta que ese
proyecto no esté en marcha, `/traffic/` da 404 y `/vm/` da 502.

## Claude (conector MCP)

stockscreener expone un servidor MCP en `https://tabkiper.com/stockscreener/mcp` (ver el README
principal, «Claude»). Claude busca los metadatos de OAuth en la raíz del dominio
(`/.well-known/oauth-authorization-server/stockscreener` y
`/.well-known/oauth-protected-resource/stockscreener/mcp`); en `conf.d/default.conf`, el bloque
HTTPS de `tabkiper.com` los reenvía a stockscreener **antes** de `location /` (si no, irían a
tabbito). Los demás `.well-known` de la raíz siguen yendo a tabbito.

Puesta en marcha, una sola vez:

```bash
# 1. Proxy: copiar conf.d/default.conf de este repositorio a /home/debian/edge/conf.d/ y recargar
cd /home/debian/edge
docker compose exec nginx nginx -t && docker compose exec nginx nginx -s reload

# 2. App: URL pública y versión nueva (la migración de la base de datos se aplica sola)
cd /home/debian/stockscreener
grep -q '^PUBLIC_URL=' .env 2>/dev/null || echo 'PUBLIC_URL=https://tabkiper.com/stockscreener' >> .env
docker compose pull && docker compose up -d

# 3. Comprobar (JSON con "https://tabkiper.com/stockscreener" y un 401 con resource_metadata)
curl -s https://tabkiper.com/.well-known/oauth-authorization-server/stockscreener; echo
curl -si -X POST https://tabkiper.com/stockscreener/mcp | grep -i '^www-authenticate'
```

Después, en claude.ai: *Configuración → Conectores → Añadir conector personalizado*, URL
`https://tabkiper.com/stockscreener/mcp`, *Conectar*, entrar con la cuenta de la app y
*Permitir*. La página `https://tabkiper.com/stockscreener/claude` muestra las apps conectadas, el
historial de consultas y los análisis guardados.

## Notas

- **Sin autenticación en el proxy** (salvo `mon.tabkiper.com`, ver arriba). El proxy no pide contraseña: tabbito usa la suya propia, y
  el screener queda accesible para cualquiera que conozca la URL. Si más adelante quieres
  restringirlo, se puede añadir un login en la propia app o limitar `/stockscreener/` por IP en
  `default.conf`, sin tocar tabbito.
- **HTTPS.** Se hace solo en `edge/`; las apps no cambian. Ver la sección "HTTPS".
- **Nombre `app`.** En `default.conf`, tabbito se alcanza como `app` (el nombre de su servicio).
  Si alguna vez añades al proxy otra red con un servicio que también se llame `app`, usa en su
  lugar el nombre del contenedor (`tabbito-app-1`).
