import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

from app import auth
from app.config import get_settings
from app.db import SessionLocal
from app.logging_setup import setup_logging
from app.models import User
from app.portfolio_web import router as portfolio_router
from app.web import ROOT, LoginRequired, router


def _log_superadmin() -> None:
    """Deja en el log si el superadmin configurado ya tiene cuenta (hay que crearla con la CLI)."""
    log = logging.getLogger(__name__)
    email = auth.normalize_email(get_settings().superadmin_email)
    if not email:
        return
    try:
        with SessionLocal() as session:
            exists = session.scalar(select(User.id).where(User.email == email)) is not None
    except Exception:
        log.exception("No se pudo comprobar la cuenta del superadmin")
        return
    log.info(
        "Superadmin: %s%s",
        email,
        "" if exists else " (sin cuenta: créala con `python -m app.cli set-password EMAIL`)",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Aquí y no al importar: uvicorn configura sus loggers después de importar la app
    setup_logging("app.log")
    logging.getLogger(__name__).info("Arranque de la aplicación")
    _log_superadmin()
    scheduler = None
    if get_settings().scheduler_enabled:
        from app.scheduler import create_scheduler

        scheduler = create_scheduler()
        scheduler.start()
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Stock Screener", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(router)
app.include_router(portfolio_router)


@app.exception_handler(LoginRequired)
async def login_required(request: Request, exc: LoginRequired) -> RedirectResponse:
    query = urlencode({"next": exc.next_url, "reason": exc.reason})
    return RedirectResponse(f"{ROOT}/login?{query}", status_code=303)


@app.get("/health")
def health() -> dict:
    with SessionLocal() as session:
        session.execute(text("SELECT 1"))
    return {"status": "ok"}
