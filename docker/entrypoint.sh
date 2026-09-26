#!/bin/sh
# Aplica las migraciones pendientes antes de arrancar (desactivable con RUN_MIGRATIONS=false).
# Cualquier otro comando se ejecuta tal cual, p. ej.:
#   docker run --rm --env-file .env IMAGEN python -m app.cli universe
set -e
if [ "${RUN_MIGRATIONS:-true}" = "true" ] && [ "$1" = "uvicorn" ]; then
    alembic upgrade head
fi
exec "$@"
