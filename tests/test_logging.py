import logging

from app.config import get_settings
from app.logging_setup import setup_logging


def _reset(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def test_logs_to_rotating_file(tmp_path, monkeypatch):
    _reset(monkeypatch, LOG_DIR=str(tmp_path / "logs"))
    try:
        setup_logging("app.log")
        logging.getLogger("app.jobs").info("hola desde las tareas")
        logging.getLogger("uvicorn.access").info("GET /screener 200")
        logging.getLogger("uvicorn.error").info("Uvicorn running")
        for h in logging.getLogger().handlers:
            h.flush()
        text = (tmp_path / "logs" / "app.log").read_text()
        assert "hola desde las tareas" in text
        assert "GET /screener 200" in text
        assert text.count("Uvicorn running") == 1  # sin duplicados por propagación
    finally:
        get_settings.cache_clear()


def test_unwritable_log_dir_falls_back_to_console(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    _reset(monkeypatch, LOG_DIR=str(blocker / "logs"))  # no se puede crear un dir bajo un fichero
    try:
        setup_logging("app.log")
        assert not any(
            isinstance(h, logging.handlers.RotatingFileHandler)
            for h in logging.getLogger().handlers
        )
    finally:
        get_settings.cache_clear()
