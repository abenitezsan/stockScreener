# Despliegue en el VPS junto a tabbito

El VPS solo expone el puerto 80, y ese puerto ya lo tiene el nginx del proyecto tabbito. La
app no publica ningún puerto: nginx le reenvía `http://<IP>/stockscreener/` a través de una red
Docker compartida llamada `web`.

```
internet :80 ──► nginx (tabbito) ──┬─ /tabbito/        ──► app:8080       (red tabbito-net)
                                   └─ /stockscreener/  ──► stockscreener:8000 (red web)
```

- **tabbito no depende de esta app.** nginx resuelve `stockscreener` en cada petición, así que
  arranca y sirve `/tabbito/` aunque el screener esté parado (en ese caso `/stockscreener/`
  devuelve 502).
- **La app no tiene login propio.** nginx la protege con usuario y contraseña (auth básica).
  Ten en cuenta que por HTTP la contraseña viaja sin cifrar.

## 1. Red compartida (una vez)

```bash
docker network create web
```

## 2. Cambios en tabbito

### `docker-compose.prod.yml`

Al servicio `nginx` se le añaden la red `web` y el fichero de contraseñas. Al final del
fichero se declara la red `web` como externa:

```yaml
  nginx:
    # ... igual que ahora ...
    volumes:
      - /home/debian/tabbito/nginx/tabbito.conf:/etc/nginx/conf.d/default.conf:ro
      - /home/debian/tabbito/nginx/logs:/var/log/nginx
      - /home/debian/tabbito/nginx/stockscreener.htpasswd:/etc/nginx/stockscreener.htpasswd:ro   # nuevo
    networks:
      - tabbito-net
      - web                                                                                        # nuevo

networks:
  tabbito-net:
  web:                  # nuevo
    external: true      # nuevo
```

### `tabbito.conf`

Pega el contenido de [`nginx-stockscreener.conf`](nginx-stockscreener.conf) **dentro** del
bloque `server { ... }`, después de la `location /tabbito/`.

### Usuario y contraseña

```bash
docker run --rm httpd:2.4-alpine htpasswd -nbB TU_USUARIO 'TU_CONTRASEÑA' \
  > /home/debian/tabbito/nginx/stockscreener.htpasswd
```

### Aplicar los cambios

```bash
cd <directorio de tabbito>
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d nginx
```

Con esto se recrea solo el contenedor de nginx; la app y la base de datos de tabbito no se
tocan. `/tabbito/` sigue funcionando y `/stockscreener/` devuelve 502 hasta el paso 3.

> **Ojo al editar `tabbito.conf` más adelante.** Está montado como fichero suelto. Muchos
> editores (y `sed -i`) guardan creando un fichero nuevo, y el contenedor sigue viendo el
> antiguo: un `nginx -s reload` no recoge el cambio. Usa
> `docker compose -f docker-compose.prod.yml restart nginx`.
> Antes, comprueba la sintaxis con
> `docker compose -f docker-compose.prod.yml exec nginx nginx -t`.

## 3. Arrancar stockscreener

En un directorio nuevo, por ejemplo `/home/debian/stockscreener`, copia el
[`docker-compose.yml`](../docker-compose.yml) del repositorio. Opcionalmente, crea un `.env`
(ver [`.env.example`](../.env.example)):

```bash
cd /home/debian/stockscreener
docker compose pull && docker compose up -d
docker compose exec stockscreener python -m app.cli universe
docker compose exec stockscreener python -m app.cli refresh bootstrap    # ~1 hora
```

Abre `http://<IP>/stockscreener/` e introduce el usuario y la contraseña del paso 2.

## Operación

| Tarea | Comando (en el directorio de stockscreener) |
|---|---|
| Actualizar a la última imagen | `docker compose pull && docker compose up -d` |
| Ver logs | `docker compose logs -f` |
| Copia de seguridad | `docker compose exec stockscreener python -m app.cli backup /data/backup.db && docker compose cp stockscreener:/data/backup.db ./backup-$(date +%F).db` |
| Parar (conserva los datos) | `docker compose down` |

Actualizar o parar stockscreener no afecta a tabbito, y al revés.
