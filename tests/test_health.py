"""Health endpoint tests.

The 'down' path is covered explicitly, not just the happy path: a health check that
reports "ok" while the database is unreachable is the specific failure this endpoint
exists to prevent, so it's the case worth pinning down in a test.
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.db import get_session
from app.main import create_app


@pytest.fixture
def app() -> FastAPI:
    return create_app()


async def _get_health(app: FastAPI):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/api/health")


async def test_health_reports_ok_when_database_reachable(app: FastAPI):
    response = await _get_health(app)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "up"


async def test_health_reports_503_when_database_unreachable(app: FastAPI):
    """A dead database must produce 503, not 200 — this is what an uptime monitor and
    (later) Nginx use to tell 'process alive' from 'able to serve requests'."""

    class BrokenSession:
        async def execute(self, *_args, **_kwargs):
            raise ConnectionError("simulated: database unreachable")

    async def broken_session():
        yield BrokenSession()

    app.dependency_overrides[get_session] = broken_session
    try:
        response = await _get_health(app)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"] == "down"


async def test_health_never_leaks_credentials(app: FastAPI):
    """Connection errors carry the DSN, and the DSN carries the password. The response
    body must never echo an exception message."""

    class LeakySession:
        async def execute(self, *_args, **_kwargs):
            raise ConnectionError("could not connect: postgresql://user:SUPERSECRET@host/db")

    async def leaky_session():
        yield LeakySession()

    app.dependency_overrides[get_session] = leaky_session
    try:
        response = await _get_health(app)
    finally:
        app.dependency_overrides.clear()

    assert "SUPERSECRET" not in response.text
