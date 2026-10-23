"""GET /api/search/meaning, with the embedder, Qdrant and the database faked."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings, get_settings
from app.db import get_session
from app.main import create_app
from app.services import chat_service, meaning_search

PASSAGE = {
    "bookId": "4407",
    "title": "الصحيفة السجادية",
    "volume": "1",
    "sequence": 52,
    "page": "48",
    "chunk": 0,
    "text": "اللهم اجعل لي نوراً",
}


@pytest.fixture
def client(monkeypatch):
    seen = {}

    def embed(settings, texts):
        seen["texts"] = texts
        return [[0.0] * 1024]

    def search(settings, vectors, limit=None, book_ids=None):
        seen["limit"], seen["books"] = limit, book_ids
        return [
            {**PASSAGE, "score": 0.7123},
            {**PASSAGE, "chunk": 1, "score": 0.7},  # another chunk of the same page
            {**PASSAGE, "sequence": 53, "page": "49", "score": 0.6},
        ]

    class Rows:
        def all(self):
            return [(4407, "الإمام زين العابدين")]

    class Session:
        async def execute(self, stmt):
            return Rows()

    monkeypatch.setattr(chat_service, "_embed", embed)
    monkeypatch.setattr(chat_service, "_search", search)
    meaning_search._cache.clear()
    meaning_search._recent.clear()
    app = create_app()
    settings = Settings(qdrant_api_key="q")
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = lambda: Session()
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    c.seen = seen
    return c


async def test_passages_with_book_author_and_page_one_per_page(client):
    r = await client.get("/api/search/meaning", params={"q": "دعاء النور", "limit": 5})
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["sequence"] for i in items] == [52, 53]  # the page's second chunk is left out
    assert items[0] == {
        "bookId": "4407",
        "title": "الصحيفة السجادية",
        "author": "الإمام زين العابدين",
        "volume": "1",
        "page": "48",
        "sequence": 52,
        "text": PASSAGE["text"],
        "score": 0.7123,
    }
    assert client.seen["texts"] == ["دعاء النور"] and client.seen["limit"] == 10


async def test_books_filter_and_cache(client):
    params = {"q": "دعاء النور", "bookId": [4407, 12]}
    await client.get("/api/search/meaning", params=params)
    assert client.seen["books"] == ["4407", "12"]
    client.seen.clear()
    await client.get("/api/search/meaning", params=params)  # the same search: from the cache
    assert not client.seen


async def test_too_many_searches_from_one_address(client):
    for _ in range(meaning_search.RATE_PER_MINUTE):
        assert (
            await client.get("/api/search/meaning", params={"q": "دعاء النور"})
        ).status_code == 200
    r = await client.get("/api/search/meaning", params={"q": "دعاء النور"})
    assert r.status_code == 429 and r.json()["code"] == "too_fast"


async def test_query_too_short_is_422(client):
    assert (await client.get("/api/search/meaning", params={"q": "ا"})).status_code == 422
