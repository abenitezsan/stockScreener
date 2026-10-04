"""Fixtures compartidas: una única app con base SQLite temporal para toda la sesión de tests."""

import os

import pytest


@pytest.fixture(scope="session")
def client(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("db") / "test.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["SCHEDULER_ENABLED"] = "false"
    os.environ["REQUEST_DELAY"] = "0"
    from app.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from app import jobs, universe
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base
    from tests.fake_provider import SPECS, FakeProvider

    assert str(db_path) in str(engine.url)
    Base.metadata.create_all(engine)

    provider = FakeProvider()
    with SessionLocal() as session:
        universe.upsert_universe(session, "TEST", [*SPECS, "ZZZ.XX"])
        jobs.refresh_fx(session, provider)
        jobs.refresh_profiles(session, provider)
        jobs.refresh_history(session, provider)
        jobs.refresh_quotes(session, provider)
        jobs.refresh_financials(session, provider)
        jobs.recompute_valuations(session)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def anon(client):
    """Cliente nuevo y sin sesión (el de `client` comparte cookies entre tests)."""
    from fastapi.testclient import TestClient

    from app import auth

    auth._failures.clear()  # el límite de intentos es por IP y todos los tests comparten la suya
    with TestClient(client.app) as c:
        yield c
