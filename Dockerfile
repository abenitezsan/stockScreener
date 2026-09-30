# syntax=docker/dockerfile:1

# --- Dependencias -------------------------------------------------------------------
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN python -m venv /venv
ENV PATH=/venv/bin:$PATH
# Primero solo pyproject: la capa de dependencias se reutiliza si solo cambia el código
COPY pyproject.toml .
RUN mkdir app && touch app/__init__.py && pip install . && pip uninstall -y stockscreener
COPY app app
RUN pip install --no-deps . \
 && find /venv -depth -type d \( -name tests -o -name testing \) -path "*site-packages/*" -exec rm -rf {} + \
 && pip uninstall -y pip

# --- Imagen final -------------------------------------------------------------------
FROM python:3.12-slim
ENV PATH=/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Madrid \
    DATABASE_URL=sqlite:////data/stockscreener.db
RUN useradd --system --uid 10001 --home-dir /app app \
 && mkdir -p /data /app/logs && chown app:app /data /app/logs
WORKDIR /app
COPY --from=build /venv /venv
COPY alembic.ini .
COPY migrations migrations
COPY app app
COPY docker/entrypoint.sh /entrypoint.sh
USER app
# Base de datos SQLite: monta aquí un volumen para que sobreviva a las actualizaciones
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"
ENTRYPOINT ["/entrypoint.sh"]
# Un solo worker: el planificador de tareas vive dentro del proceso web
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
