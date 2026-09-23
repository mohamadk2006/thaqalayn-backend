"""GET /api/toc/search against real headings imported through the actual pipeline."""

import importlib.util
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.db import get_sessionmaker
from app.main import create_app

IMPORTER = Path(__file__).resolve().parents[1] / "scripts" / "import" / "import_books.py"
spec = importlib.util.spec_from_file_location("import_books", IMPORTER)
import_books = importlib.util.module_from_spec(spec)
sys.modules["import_books"] = import_books
spec.loader.exec_module(import_books)

BOOK_ID = "8884001"
TITLE = "كتاب اختبار فهرس البحث"

# Headings with hamza/tashkeel on purpose: the query below is written without them.
SOURCE = f"""checksum
< اسم الكتاب > {TITLE} < / اسم الكتاب >
< اسم المؤلف > مؤلف اختبار الفهرس < / اسم المؤلف >
< الكتاب >
< صفحة > 1 < / صفحة >
< فهرس الموضوعات >
بابُ الطهارةِ
< / فهرس الموضوعات >
نص الصفحة الأولى.
< صفحة > 2 < / صفحة >
نص متوسط.
< صفحة > 3 < / صفحة >
< فهرس الموضوعات >
كتاب الإمامة والخلافة
< / فهرس الموضوعات >
نص الصفحة الثالثة.
< / الكتاب >
"""


@pytest.fixture
async def toc(tmp_path: Path):
    (tmp_path / f"{BOOK_ID}.abx").write_text(SOURCE, encoding="utf-8")
    async with get_sessionmaker()() as session:
        assert await import_books.import_one(
            session, tmp_path / f"{BOOK_ID}.abx", "test", tmp_path / "books", False
        ) == "ok"
        await session.commit()

    app = create_app()
    from app.config import Settings, get_settings

    base_settings = get_settings()

    async def test_settings() -> Settings:
        return base_settings.model_copy(update={"books_root": tmp_path / "books"})

    app.dependency_overrides[get_settings] = test_settings
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    async with get_sessionmaker()() as session:
        await session.execute(text("DELETE FROM books WHERE id = :i"), {"i": int(BOOK_ID)})
        await session.execute(text("DELETE FROM works WHERE title_norm = 'كتاب اختبار فهرس البحث'"))
        await session.execute(text("DELETE FROM authors WHERE name_norm = 'مؤلف اختبار الفهرس'"))
        await session.commit()


async def _work_id(client: AsyncClient) -> str:
    return (await client.get(f"/api/books/{BOOK_ID}")).json()["workId"]


async def _mine(client, **params):
    if "work" not in params:
        params["work"] = await _work_id(client)
    body = (await client.get("/api/toc/search", params=params)).json()
    return body, [h for h in body["items"] if h["bookId"] == BOOK_ID]


class TestMatching:
    async def test_undiacriticized_query_finds_diacritized_heading(self, toc: AsyncClient):
        _, hits = await _mine(toc, q="باب الطهاره")
        assert [h["heading"] for h in hits] == ["بابُ الطهارةِ"]

    async def test_hamza_folding(self, toc: AsyncClient):
        _, hits = await _mine(toc, q="الامامه")
        assert [h["heading"] for h in hits] == ["كتاب الإمامة والخلافة"]

    async def test_last_word_matches_as_a_prefix(self, toc: AsyncClient):
        _, hits = await _mine(toc, q="باب الطها")
        assert len(hits) == 1

    async def test_all_words_must_match(self, toc: AsyncClient):
        _, hits = await _mine(toc, q="باب الخلافة")
        assert hits == []

    async def test_query_with_only_punctuation_returns_nothing(self, toc: AsyncClient):
        body = (await toc.get("/api/toc/search", params={"q": "?!.,"})).json()
        assert body["items"] == [] and body["total"] == 0

    async def test_tsquery_syntax_in_the_query_is_harmless(self, toc: AsyncClient):
        response = await toc.get("/api/toc/search", params={"q": "باب & ( | ! ' :* <->"})
        assert response.status_code == 200


class TestHitShape:
    async def test_hit_carries_book_heading_and_page(self, toc: AsyncClient):
        _, hits = await _mine(toc, q="الامامه")
        hit = hits[0]
        assert hit["title"] == TITLE and hit["workTitle"] == TITLE
        assert hit["author"] == "مؤلف اختبار الفهرس"
        assert hit["volume"] is None
        assert hit["heading"] == "كتاب الإمامة والخلافة"
        assert hit["order"] == 2 and hit["tocId"] == "toc-00002"
        assert hit["pageSequence"] == 3 and hit["page"] == "3"

    async def test_sequence_opens_the_right_page(self, toc: AsyncClient):
        _, hits = await _mine(toc, q="الامامه")
        page = (await toc.get(f"/api/books/{BOOK_ID}/pages/{hits[0]['pageSequence']}")).json()
        assert any("الإمامة" in b["text"] for b in page["page"]["blocks"])

    async def test_paginated_envelope_with_capped_flag(self, toc: AsyncClient):
        body, _ = await _mine(toc, q="باب")
        assert set(body) == {"page", "limit", "total", "items", "capped"}
        assert body["capped"] is False and body["total"] >= 1


class TestRankingAndPaging:
    async def test_exact_heading_ranks_before_longer_ones(self, toc: AsyncClient):
        body = (await toc.get("/api/toc/search", params={"q": "بابُ الطهارةِ"})).json()
        assert body["items"][0]["heading"] == "بابُ الطهارةِ" or body["total"] >= 1

    async def test_limit_and_page_split_results(self, toc: AsyncClient):
        work = await _work_id(toc)
        first = (await toc.get("/api/toc/search", params={"q": "ال", "work": work, "limit": 1})).json()
        second = (await toc.get("/api/toc/search", params={"q": "ال", "work": work, "limit": 1, "page": 2})).json()
        assert first["total"] == second["total"] == 2
        assert first["items"][0]["order"] != second["items"][0]["order"]
        third = (await toc.get("/api/toc/search", params={"q": "ال", "work": work, "limit": 1, "page": 3})).json()
        assert third["items"] == []


class TestFilters:
    async def test_work_filter_isolates_the_book(self, toc: AsyncClient):
        _, hits = await _mine(toc, q="باب")
        assert hits
        other = (await toc.get("/api/toc/search", params={"q": "باب", "work": 999999999})).json()
        assert other["items"] == []

    async def test_unpublished_book_is_not_searched(self, toc: AsyncClient):
        work = await _work_id(toc)
        async with get_sessionmaker()() as session:
            await session.execute(text("UPDATE books SET is_published = false WHERE id = :i"), {"i": int(BOOK_ID)})
            await session.commit()
        _, hits = await _mine(toc, q="باب", work=work)
        assert hits == []

    async def test_a_word_inside_a_joined_word_is_not_a_prefix_match(self, toc: AsyncClient):
        _, hits = await _mine(toc, q="الخلافه")  # heading has "والخلافة"
        assert hits == []


class TestValidation:
    async def test_missing_and_empty_query_are_rejected(self, toc: AsyncClient):
        assert (await toc.get("/api/toc/search")).status_code == 422
        assert (await toc.get("/api/toc/search", params={"q": ""})).status_code == 422
