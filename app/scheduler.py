"""Tareas programadas dentro del propio proceso web (sin Redis ni Celery).

Ejecuta uvicorn con un solo worker: con varios, cada uno lanzaría su propio planificador.
"""

import logging
from collections.abc import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session

from app import jobs
from app.config import get_settings
from app.db import SessionLocal

log = logging.getLogger(__name__)


def _run(job: Callable[[Session], object]) -> Callable[[], None]:
    def wrapper() -> None:
        log.info("Inicio de %s", job.__name__)
        try:
            with SessionLocal() as session:
                result = job(session)
            log.info("Fin de %s: %s", job.__name__, result)
        except Exception:
            log.exception("Error en %s", job.__name__)

    wrapper.__name__ = wrapper.__qualname__ = job.__name__
    return wrapper


def create_scheduler() -> BackgroundScheduler:
    tz = get_settings().timezone
    scheduler = BackgroundScheduler(
        timezone=tz, job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600}
    )
    # Cotizaciones cada 20 min con alguna bolsa abierta (Europa 9-17:30, América 15:30-22 h)
    scheduler.add_job(
        _run(jobs.refresh_quotes),
        "cron",
        day_of_week="mon-fri",
        hour="9-22",
        minute="*/20",
        id="quotes",
    )
    # Fundamentales, histórico y valoración tras el cierre americano
    scheduler.add_job(
        _run(jobs.nightly), "cron", day_of_week="mon-fri", hour=23, minute=15, id="nightly"
    )
    # Cuentas anuales (cambian como mucho trimestralmente)
    scheduler.add_job(_run(jobs.weekly), "cron", day_of_week="sat", hour=10, id="weekly")
    return scheduler
