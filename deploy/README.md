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
│   └── logs/                # app.log (web y tareas programadas) y cli.log, con rotación
└── edge/                    # copia de deploy/edge/ de este repositorio
    ├── docker-compose.yml
    ├── conf.d/default.conf  # rutas de las dos apps
    ├── htpasswd/            # contraseñas (auth básica)
    └── logs/                # access.log / error.log de nginx
```

## Migración paso a paso

El corte de tabbito dura solo unos segundos, entre los pasos 4 y 5.

### 1. Arrancar stockscreener

Crea `/home/debian/stockscreener/` con el `docker-compose.yml` del repositorio y ejecuta:

```bash
cd /home/debian/stockscreener
mkdir -p logs && sudo chown 10001:10001 logs       # la app corre con uid 10001
docker compose pull && docker compose up -d        # crea la red stockscreener-net
```

Si te saltas el `chown`, la app arranca igual pero solo escribe los logs en consola
(`docker compose logs`), y avisa de ello al arrancar.

### 2. Preparar el proxy

```bash
mkdir -p /home/debian/edge && cp -r deploy/edge/. /home/debian/edge/    # desde un clon del repo
cd /home/debian/edge
docker run --rm httpd:2.4-alpine htpasswd -nbB TU_USUARIO 'TU_CONTRASEÑA' > htpasswd/stockscreener
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
- `http://<IP>/stockscreener/` → pide usuario y contraseña, y muestra el screener.

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
| Copia de seguridad del screener | `stockscreener/` | `docker compose exec stockscreener python -m app.cli backup /data/backup.db && docker compose cp stockscreener:/data/backup.db ./backup-$(date +%F).db` |
| Cambiar la configuración de nginx | `edge/` | edita `conf.d/default.conf`, luego `docker compose exec nginx nginx -t && docker compose exec nginx nginx -s reload` |
| Cambiar la contraseña | `edge/` | vuelve a generar `htpasswd/stockscreener` (paso 2); no hace falta reiniciar |
| Ver peticiones | `edge/` | `tail -f logs/access.log` |

Para añadir otra app en el futuro:
1. Dale a su red un nombre fijo en su `docker-compose.yml`.
2. Añade esa red como `external` en `edge/docker-compose.yml`.
3. Añade su `location` en `conf.d/default.conf`.
4. Ejecuta `docker compose up -d` en `edge/`.

## Notas

- **Plain HTTP.** Por HTTP, la contraseña del screener viaja sin cifrar. Cuando quieras HTTPS,
  solo hay que tocar `edge/`; las apps no cambian.
- **Nombre `app`.** En `default.conf`, tabbito se alcanza como `app` (el nombre de su servicio).
  Si alguna vez añades al proxy otra red con un servicio que también se llame `app`, usa en su
  lugar el nombre del contenedor (`tabbito-app-1`).
