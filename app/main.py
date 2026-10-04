import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.config import get_settings
from app.db import SessionLocal
from app.logging_setup import setup_logging
from app.web import ROOT, LoginRequired, router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Aquí y no al importar: uvicorn configura sus loggers después de importar la app
    setup_logging("app.log")
    logging.getLogger(__name__).info("Arranque de la aplicación")
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


@app.exception_handler(LoginRequired)
async def login_required(request: Request, exc: LoginRequired) -> RedirectResponse:
    query = urlencode({"next": exc.next_url, "reason": exc.reason})
    return RedirectResponse(f"{ROOT}/login?{query}", status_code=303)


@app.get("/health")
def health() -> dict:
    with SessionLocal() as session:
        session.execute(text("SELECT 1"))
    return {"status": "ok"}
