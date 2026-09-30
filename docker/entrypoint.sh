#!/bin/sh
# Aplica las migraciones pendientes antes de arrancar (desactivable con RUN_MIGRATIONS=false).
# Cualquier otro comando se ejecuta tal cual, p. ej.:
#   docker run --rm --env-file .env IMAGEN python -m app.cli universe
set -e

# SQLite necesita escribir en la carpeta de la base de datos (crea los ficheros -wal y -shm).
# Con una carpeta del host sin permisos, el error de SQLite es críptico: se avisa claramente.
case "$DATABASE_URL" in
    sqlite:///*)
        db_dir=$(dirname "${DATABASE_URL#sqlite:///}")
        mkdir -p "$db_dir" 2>/dev/null || true
        if [ ! -w "$db_dir" ]; then
            echo "ERROR: el usuario $(id -u) no puede escribir en $db_dir." >&2
            echo "En el host: sudo chown $(id -u):$(id -u) <carpeta montada en $db_dir>" >&2
            exit 1
        fi
        ;;
esac

if [ "${RUN_MIGRATIONS:-true}" = "true" ] && [ "$1" = "uvicorn" ]; then
    alembic upgrade head
fi
exec "$@"
