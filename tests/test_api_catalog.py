"""Integration tests for the read API: works, books, download, metadata.

Runs against real database rows created by the importer's own code path — not hand-built
fixtures — so these tests exercise the same eager-loading and aggregation queries a real
deployment hits, including the two bugs a hand-rolled fixture would have hidden:
missing eager-loads (async SQLAlchemy can't lazy-load outside its greenlet, so a missed
relationship is a 500) and GROUP BY plus joinedload not composing in the same query.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.main import create_app

IMPORTER = Path(__file__).resolve().parents[1] / "scripts" / "import" / "import_books.py"
spec = importlib.util.spec_from_file_location("import_books", IMPORTER)
import_books = importlib.util.module_from_spec(spec)
sys.modules["import_books"] = import_books
spec.loader.exec_module(import_books)

BOOK_A = "8881001"  # single-volume work
BOOK_B1, BOOK_B2 = "8882001", "8882002"  # two volumes of one work

SOURCE_A = """checksum
< اسم الكتاب > كتاب الاختبار الأول < / اسم الكتاب >
< اسم المؤلف > مؤلف الاختبار < / اسم المؤلف >
< مجموعة > مصادر التفسير عند الشيعة < / مجموعة >
< الكتاب >
< صفحة > 1 < / صفحة >
< فهرس الموضوعات >
الباب الأول
< / فهرس الموضوعات >
نص عن الإمام الصادق عليه السلام.
< / الكتاب >
"""

SOURCE_B1 = """checksum
< اسم الكتاب > كتاب الاختبار الثاني < / اسم الكتاب >
< اسم المؤلف > مؤلف آخر < / اسم المؤلف >
< جزء > 1 < / جزء >
< مجموعة > فقه المذهب الحنفي < / مجموعة >
< الكتاب >
< صفحة > 1 < / صفحة >
< فهرس الموضوعات >
الجزء الأول
< / فهرس الموضوعات >
نص الجزء الأول.
< / الكتاب >
"""

SOURCE_B2 = SOURCE_B1.replace("< جزء > 1 < / جزء >", "< جزء > 2 < / جزء >").replace(
    "نص الجزء الأول.", "نص الجزء الثاني."
)


@pytest.fixture
async def imported(tmp_path: Path):
    """Import three synthetic books through the real importer, yield an ASGI client
    against the live database, then remove everything this test created."""
    for name, source in [(BOOK_A, SOURCE_A), (BOOK_B1, SOURCE_B1), (BOOK_B2, SOURCE_B2)]:
        (tmp_path / f"{name}.abx").write_text(source, encoding="utf-8")

    books_root = tmp_path / "books"
    async with get_sessionmaker()() as session:
        for name in (BOOK_A, BOOK_B1, BOOK_B2):
            result = await import_books.import_one(
                session, tmp_path / f"{name}.abx", "test", books_root, False
            )
            assert result == "ok", name

    app = create_app()

    base_settings = get_settings()

    async def test_settings() -> Settings:
        return base_settings.model_copy(update={"books_root": books_root})

    app.dependency_overrides[get_settings] = test_settings

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    async with get_sessionmaker()() as session:
        await session.execute(
            text("DELETE FROM books WHERE id = ANY(:ids)"),
            {"ids": [int(BOOK_A), int(BOOK_B1), int(BOOK_B2)]},
        )
        await session.execute(
            text(
                "DELETE FROM works WHERE title_norm IN "
                "('كتاب الاختبار الاول', 'كتاب الاختبار الثاني')"
            )
        )
        await session.execute(
            text("DELETE FROM authors WHERE name_norm IN ('مؤلف الاختبار', 'مؤلف اخر')")
        )
        await session.commit()


class TestBooksList:
    async def test_returns_paginated_envelope(self, imported: AsyncClient):
        response = await imported.get("/api/books", params={"limit": 2})
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"page", "limit", "total", "items"}
        assert body["limit"] == 2
        assert len(body["items"]) <= 2

    async def test_filters_by_subject(self, imported: AsyncClient):
        """Narrowed with `work` on top of `subject`, deliberately -- the dev database
        is a live, actively-growing import (thousands of real fiqh-hanafi books
        alongside these two synthetic ones), so an unscoped subject=fiqh-hanafi page
        can legitimately not contain these two IDs on page 1. Combining with `work`
        makes the assertion exact regardless of how much other real data exists."""
        work_id = (await imported.get(f"/api/books/{BOOK_B1}")).json()["workId"]

        response = await imported.get(
            "/api/books", params={"subject": "fiqh-hanafi", "work": work_id}
        )
        book_ids = {item["bookId"] for item in response.json()["items"]}
        assert book_ids == {BOOK_B1, BOOK_B2}

        response = await imported.get(
            "/api/books", params={"subject": "tafsir-shia", "work": work_id}
        )
        assert response.json()["items"] == []  # this work is fiqh-hanafi, not tafsir-shia

    async def test_limit_is_bounded(self, imported: AsyncClient):
        response = await imported.get("/api/books", params={"limit": 1000})
        assert response.status_code == 422


class TestBookDetail:
    async def test_returns_full_contract_shape(self, imported: AsyncClient):
        response = await imported.get(f"/api/books/{BOOK_A}")
        assert response.status_code == 200
        body = response.json()
        assert body["bookId"] == BOOK_A
        assert body["title"] == "كتاب الاختبار الأول"
        assert body["subjectId"] == "tafsir-shia"
        assert body["volume"] is None  # no جزء tag on this source

    async def test_unknown_id_is_404(self, imported: AsyncClient):
        response = await imported.get("/api/books/999999999")
        assert response.status_code == 404


class TestWorks:
    async def test_multi_volume_work_groups_correctly(self, imported: AsyncClient):
        """The whole point of works vs. books: two files with the same (title, author)
        collapse into one work with two nested volumes."""
        detail = await imported.get(f"/api/books/{BOOK_B1}")
        work_id = detail.json()["workId"]

        response = await imported.get(f"/api/works/{work_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["volumeCount"] == 2
        volumes = {v["bookId"]: v["volume"] for v in body["volumes"]}
        assert volumes == {BOOK_B1: 1, BOOK_B2: 2}
        # Regression: get_work's volumes loop once forgot to fill in subjectTitle after
        # batch-loading it, leaving it null in every volume despite subjectId being set.
        for volume in body["volumes"]:
            assert volume["subjectTitle"] is not None

    async def test_work_filters_survive_pagination(self, imported: AsyncClient):
        """Same fixture-vs-live-database issue as test_filters_by_subject: with the
        import running, the dev database now holds thousands of real fiqh-hanafi
        works, so the list endpoint's own subject=fiqh-hanafi page 1 is no longer a
        reliable place to find these two synthetic ones. Combining with `author` (a
        name unique to this fixture) makes the assertion exact regardless of how much
        other data exists -- and, unlike calling the detail endpoint directly, this
        still genuinely tests the LIST endpoint's filter mechanics rather than
        sidestepping them."""
        async with get_sessionmaker()() as session:
            author_id = await session.scalar(
                text("SELECT id FROM authors WHERE name_norm = 'مؤلف اخر'")
            )
        assert author_id is not None

        response = await imported.get(
            "/api/works", params={"subject": "fiqh-hanafi", "author": author_id}
        )
        titles = {w["title"] for w in response.json()["items"]}
        assert titles == {"كتاب الاختبار الثاني"}

        response = await imported.get(
            "/api/works", params={"subject": "tafsir-shia", "author": author_id}
        )
        assert response.json()["items"] == []


class TestDownload:
    async def test_downloads_the_registered_file(self, imported: AsyncClient):
        response = await imported.get(f"/api/books/{BOOK_A}/download")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        body = response.json()
        assert body["bookId"] == BOOK_A
        assert "الإمام الصادق" in body["chapters"][0]["sections"][0]["paragraphs"][0]["text"]

    async def test_content_length_matches_stored_size(self, imported: AsyncClient):
        detail = (await imported.get(f"/api/books/{BOOK_A}")).json()
        response = await imported.get(f"/api/books/{BOOK_A}/download")
        assert int(response.headers["content-length"]) == detail["sizeBytes"]

    async def test_unpublished_or_unknown_book_404s(self, imported: AsyncClient):
        response = await imported.get("/api/books/999999999/download")
        assert response.status_code == 404

    async def test_traversal_via_manipulated_content_path_is_rejected(self, imported: AsyncClient):
        """content_path is written by our own importer, not user input — but the spec
        explicitly requires the endpoint defend structurally, not just trust its source.
        Simulate a corrupted/malicious row directly and confirm the guard still fires."""
        async with get_sessionmaker()() as session:
            await session.execute(
                text("UPDATE books SET content_path = :p WHERE id = :i"),
                {"p": "../../../../../../etc/passwd", "i": int(BOOK_A)},
            )
            await session.commit()
        try:
            response = await imported.get(f"/api/books/{BOOK_A}/download")
            assert response.status_code in (404, 500)
            assert "escapes" in response.text or response.status_code == 404
        finally:
            async with get_sessionmaker()() as session:
                await session.execute(
                    text("UPDATE books SET content_path = :p WHERE id = :i"),
                    {"p": f"{BOOK_A}.json", "i": int(BOOK_A)},
                )
                await session.commit()


class TestMetadata:
    async def test_categories_lists_all_subjects(self, imported: AsyncClient):
        response = await imported.get("/api/categories")
        assert response.status_code == 200
        assert len(response.json()) == 40  # Shamela's own 39, plus our "other" catch-all

    async def test_languages(self, imported: AsyncClient):
        response = await imported.get("/api/languages")
        codes = {row["code"] for row in response.json()}
        assert codes == {"ar", "fa"}

    async def test_authors_includes_imported_authors(self, imported: AsyncClient):
        response = await imported.get("/api/authors")
        names = {row["name"] for row in response.json()}
        assert "مؤلف الاختبار" in names
