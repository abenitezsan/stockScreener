"""Logs a consola y, si LOG_DIR está definido, también a fichero con rotación."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import get_settings

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(filename: str) -> None:
    """`filename`: app.log para el proceso web y las tareas programadas; cli.log para la CLI.
    Ficheros distintos porque varios procesos rotando el mismo fichero se pisan."""
    settings = get_settings()
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    file_error = None
    if settings.log_dir:
        path = Path(settings.log_dir) / filename
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(
                RotatingFileHandler(
                    path,
                    maxBytes=settings.log_max_mb * 1024 * 1024,
                    backupCount=settings.log_backups,
                    encoding="utf-8",
                )
            )
        except OSError as exc:  # p. ej. carpeta del host sin permisos: la app arranca igual
            file_error = exc
    formatter = logging.Formatter(FORMAT)
    for handler in handlers:
        handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = handlers
    root.setLevel(settings.log_level.upper())
    # uvicorn configura sus propios loggers sin propagar al raíz: se les dan los mismos handlers
    # para que las peticiones y los errores del servidor también lleguen al fichero.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = handlers if name != "uvicorn.error" else []
        logger.propagate = name == "uvicorn.error"
    if file_error:
        root.warning(
            "No se puede escribir el log en %s (%s); solo consola", settings.log_dir, file_error
        )
