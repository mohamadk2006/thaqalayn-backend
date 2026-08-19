"""Integration tests for GET /api/search, against real data imported through the actual
pipeline -- same rationale as test_api_catalog.py: this is what catches eager-loading
gaps and query-shape bugs a hand-built fixture would hide.
"""

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

BOOK_ID = "8883001"

# Deliberately uses full tashkeel in the source, undiacriticized in the test query below
# -- this pair is the whole reason the normalizer and the snippet locator exist.
SOURCE = """checksum
< اسم الكتاب > كتاب اختبار البحث < / اسم الكتاب >
< اسم المؤلف > مؤلف الاختبار الثالث < / اسم المؤلف >
< مجموعة > مصادر الحديث الشيعية ( القسم العام ) < / مجموعة >
< الكتاب >
< صفحة > 1 < / صفحة >
< فهرس الموضوعات >
باب في فضل الإمام
< / فهرس الموضوعات >
قال الإمامُ الصادقُ عليه السلام: العلمُ نورٌ يقذفه اللهُ في قلبِ من يشاء.
< صفحة > 2 < / صفحة >
نص لا علاقة له بموضوع البحث إطلاقا في هذه الصفحة.
< / الكتاب >
"""


@pytest.fixture
async def imported(tmp_path: Path):
    (tmp_path / f"{BOOK_ID}.abx").write_text(SOURCE, encoding="utf-8")
    books_root = tmp_path / "books"
    async with get_sessionmaker()() as session:
        result = await import_books.import_one(
            session, tmp_path / f"{BOOK_ID}.abx", "test", books_root, False
        )
    assert result == "ok"

    app = create_app()
    from app.config import Settings, get_settings

    base_settings = get_settings()

    async def test_settings() -> Settings:
        return base_settings.model_copy(update={"books_root": books_root})

    app.dependency_overrides[get_settings] = test_settings

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    async with get_sessionmaker()() as session:
        await session.execute(text("DELETE FROM books WHERE id = :i"), {"i": int(BOOK_ID)})
        await session.execute(
            text("DELETE FROM works WHERE title_norm = 'كتاب اختبار البحث الثالث'")
        )
        await session.execute(
            text("DELETE FROM authors WHERE name_norm = 'مولف الاختبار الثالث'")
        )
        await session.commit()


async def _work_id(client: AsyncClient) -> str:
    """This fixture's own work id -- every search assertion below is scoped to it,
    since an unscoped query for a common religious phrase competes against tens of
    thousands of real matches in the live 18,796-book corpus and has no guarantee of
    landing this one synthetic hit on page 1 (the exact issue found and fixed once
    already in test_api_catalog.py)."""
    detail = await client.get(f"/api/books/{BOOK_ID}")
    return detail.json()["workId"]


class TestSearchFindsUndiacriticizedQuery:
    """The whole point: search without hamza/tashkeel finds text written with them."""

    async def test_undiacriticized_query_matches(self, imported: AsyncClient):
        work_id = await _work_id(imported)
        response = await imported.get(
            "/api/search", params={"q": "الامام الصادق", "work": work_id}
        )
        assert response.status_code == 200
        hits = [h for h in response.json()["items"] if h["bookId"] == BOOK_ID]
        assert len(hits) == 1

    async def test_hit_carries_full_contract_shape(self, imported: AsyncClient):
        work_id = await _work_id(imported)
        response = await imported.get(
            "/api/search", params={"q": "الامام الصادق", "work": work_id}
        )
        hit = next(h for h in response.json()["items"] if h["bookId"] == BOOK_ID)
        assert hit["workTitle"] == "كتاب اختبار البحث"
        assert hit["author"] == "مؤلف الاختبار الثالث"
        assert hit["page"] == 1
        assert hit["sectionTitle"] == "باب في فضل الإمام"
        assert hit["subjectId"] == "hadith"
        assert hit["score"] > 0

    async def test_snippet_preserves_original_tashkeel(self, imported: AsyncClient):
        """The core design point of the whole snippet mechanism: the result must show
        real diacritized text, never the stripped text search_tsv is built from."""
        work_id = await _work_id(imported)
        response = await imported.get(
            "/api/search", params={"q": "الامام الصادق", "work": work_id}
        )
        hit = next(h for h in response.json()["items"] if h["bookId"] == BOOK_ID)
        assert "الإمامُ" in hit["snippet"] or "الإمام" in hit["snippet"]
        # A plain, undiacritized hamza-free spelling must NOT be what's shown --
        # that would mean the snippet came from normalized text, not the original.
        assert "الامام الصادق" not in hit["snippet"]

    async def test_highlight_offsets_are_accurate(self, imported: AsyncClient):
        work_id = await _work_id(imported)
        response = await imported.get(
            "/api/search", params={"q": "الامام الصادق", "work": work_id}
        )
        hit = next(h for h in response.json()["items"] if h["bookId"] == BOOK_ID)
        assert hit["matchStart"] is not None and hit["matchEnd"] is not None
        highlighted = hit["snippet"][hit["matchStart"] : hit["matchEnd"]]
        # The highlighted slice must itself contain both query words, in order.
        assert "الإمام" in highlighted or "الامام" in highlighted
        assert "الصادق" in highlighted


class TestSearchDoesNotMatchUnrelatedContent:
    async def test_page_two_is_not_returned(self, imported: AsyncClient):
        """Page 2's text has no relation to the query -- confirms search doesn't just
        return every page of a book that happens to have one matching page elsewhere."""
        work_id = await _work_id(imported)
        response = await imported.get(
            "/api/search", params={"q": "الامام الصادق", "work": work_id}
        )
        pages = [h["page"] for h in response.json()["items"] if h["bookId"] == BOOK_ID]
        assert pages == [1]


class TestPaginationAndFilters:
    async def test_response_is_paginated_envelope(self, imported: AsyncClient):
        response = await imported.get("/api/search", params={"q": "الامام الصادق"})
        body = response.json()
        assert set(body) == {"page", "limit", "total", "items"}
        assert body["total"] >= 1

    async def test_filters_by_subject(self, imported: AsyncClient):
        work_id = await _work_id(imported)
        response = await imported.get(
            "/api/search",
            params={"q": "الامام الصادق", "work": work_id, "subject": "hadith"},
        )
        assert any(h["bookId"] == BOOK_ID for h in response.json()["items"])

        response = await imported.get(
            "/api/search",
            params={"q": "الامام الصادق", "work": work_id, "subject": "tibb"},
        )
        assert not any(h["bookId"] == BOOK_ID for h in response.json()["items"])

    async def test_filters_by_work(self, imported: AsyncClient):
        detail = (await imported.get(f"/api/books/{BOOK_ID}")).json()
        response = await imported.get(
            "/api/search", params={"q": "الامام الصادق", "work": detail["workId"]}
        )
        body = response.json()
        assert all(h["workId"] == detail["workId"] for h in body["items"])
        assert any(h["bookId"] == BOOK_ID for h in body["items"])


class TestValidation:
    async def test_empty_query_is_rejected(self, imported: AsyncClient):
        response = await imported.get("/api/search", params={"q": ""})
        assert response.status_code == 422

    async def test_missing_query_is_rejected(self, imported: AsyncClient):
        response = await imported.get("/api/search")
        assert response.status_code == 422

    async def test_query_with_no_matches_returns_empty_not_error(self, imported: AsyncClient):
        response = await imported.get(
            "/api/search", params={"q": "زركشوكاتوكاتوباستان"}
        )
        assert response.status_code == 200
        assert response.json()["items"] == []
        assert response.json()["total"] == 0
