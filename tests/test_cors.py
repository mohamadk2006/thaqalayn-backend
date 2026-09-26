"""The public website (www.thaqalaynlibrary.com) calls the API from the browser, so the API
must answer its CORS checks -- and only its, plus localhost for developing the site."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        yield c


@pytest.mark.parametrize("origin", [
    "https://www.thaqalaynlibrary.com",
    "https://thaqalaynlibrary.com",
    "http://localhost:5173",
    "http://127.0.0.1:8080",
    "http://localhost",
])
async def test_allowed_origins_can_read(client, origin):
    r = await client.get("/api/health", headers={"Origin": origin})
    assert r.headers.get("access-control-allow-origin") == origin
    assert "access-control-allow-credentials" not in r.headers


@pytest.mark.parametrize("origin", [
    "https://evil.example.com",
    "https://www.thaqalaynlibrary.com.evil.com",
    "http://localhost.evil.com",
    "http://www.thaqalaynlibrary.com",  # plain http is not the site
])
async def test_other_origins_are_not_allowed(client, origin):
    r = await client.get("/api/health", headers={"Origin": origin})
    assert "access-control-allow-origin" not in r.headers


async def test_preflight_allows_get_with_if_none_match(client):
    r = await client.options("/api/catalog/version", headers={
        "Origin": "https://www.thaqalaynlibrary.com",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "if-none-match",
    })
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "https://www.thaqalaynlibrary.com"
    assert "GET" in r.headers["access-control-allow-methods"]


async def test_preflight_refuses_writes(client):
    r = await client.options("/api/books", headers={
        "Origin": "https://www.thaqalaynlibrary.com",
        "Access-Control-Request-Method": "POST",
    })
    assert r.status_code == 400


async def test_etag_is_readable_by_the_site(client):
    r = await client.get("/api/catalog/version", headers={"Origin": "https://www.thaqalaynlibrary.com"})
    assert "etag" in r.headers.get("access-control-expose-headers", "").lower()
