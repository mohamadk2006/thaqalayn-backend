"""Search matches the words as typed: no stemming (SEARCH_TS_CONFIG = 'simple').

The real complaint this guards: "الاغتسال ليلا" returned "الاغتسال بالليل" and
"اغتسالها بالليل" under the Snowball 'arabic' stemmer. Normalization (tashkeel, hamza
forms) must still apply -- exact words, not exact bytes.

Also covers app/services/search_reindex.py, the tool that rebuilds the index from the
books' JSON files: building one book into pages_exact must give exactly the rows and
tsvectors the importer itself writes.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.db import get_sessionmaker
from app.main import create_app
from app.services import search_reindex as R
from app.services.arabic import find_original_match, normalize

IMPORTER = Path(__file__).resolve().parents[1] / "scripts" / "import" / "import_books.py"
spec = importlib.util.spec_from_file_location("import_books", IMPORTER)
import_books = importlib.util.module_from_spec(spec)
sys.modules["import_books"] = import_books
spec.loader.exec_module(import_books)

BOOK_ID = "8884001"

SOURCE = """checksum
< اسم الكتاب > كتاب اختبار البحث الحرفي < / اسم الكتاب >
< اسم المؤلف > مؤلف اختبار البحث الحرفي < / اسم المؤلف >
< الكتاب >
< صفحة > 1 < / صفحة >
ويُستحبُّ الاغتسالُ ليلاً في شهر رمضان.
< صفحة > 2 < / صفحة >
وكذلك الاغتسال بالليل للمرأة، ويجوز اغتسالها بالليل.
< صفحة > 3 < / صفحة >
قال الإمامُ الصادقُ عليه السلام: والليلِ إذا يغشى.
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
        await session.commit()

    from app.config import Settings, get_settings

    app = create_app()

    def test_settings() -> Settings:
        return get_settings().model_copy(update={"books_root": books_root})

    app.dependency_overrides[get_settings] = test_settings
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.books_root = books_root
        yield client

    async with get_sessionmaker()() as session:
        await session.execute(text(f"DROP TABLE IF EXISTS {R.TARGET}"))
        await session.execute(text("DELETE FROM books WHERE id = :i"), {"i": int(BOOK_ID)})
        await session.execute(text("DELETE FROM works WHERE title_norm = :t"),
                              {"t": normalize("كتاب اختبار البحث الحرفي")})
        await session.execute(text("DELETE FROM authors WHERE name_norm = :n"),
                              {"n": normalize("مؤلف اختبار البحث الحرفي")})
        await session.commit()


async def _search(client: AsyncClient, query: str) -> list[dict]:
    # Scoped to this book's work: on a database holding the real library, an unscoped
    # common phrase would compete with thousands of real hits (see test_api_search.py).
    work_id = (await client.get(f"/api/books/{BOOK_ID}")).json()["workId"]
    r = await client.get("/api/search", params={"q": query, "work": work_id})
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def _pages_for(client: AsyncClient, query: str) -> list[int]:
    return sorted(h["pageSequence"] for h in await _search(client, query))


class TestExactWords:
    async def test_other_forms_of_the_words_do_not_match(self, imported: AsyncClient):
        # Page 2 has "الاغتسال بالليل" and "اغتسالها بالليل": both used to match.
        assert await _pages_for(imported, "الاغتسال ليلا") == [1]

    async def test_each_form_finds_only_itself(self, imported: AsyncClient):
        assert await _pages_for(imported, "اغتسالها بالليل") == [2]
        assert await _pages_for(imported, "بالليل") == [2]

    async def test_a_word_does_not_match_inside_a_longer_word(self, imported: AsyncClient):
        # "الليل" occurs only attached: "بالليل" (page 2) and "والليل" (page 3).
        assert await _pages_for(imported, "الليل") == []

    async def test_normalization_still_applies(self, imported: AsyncClient):
        # Undiacritized query, plain alef, no tanween: still finds the diacritized text.
        assert await _pages_for(imported, "الامام الصادق") == [3]
        assert await _pages_for(imported, "الاغتسال ليلاً") == [1]

    async def test_snippet_highlights_the_whole_matched_words(self, imported: AsyncClient):
        [hit] = await _search(imported, "الاغتسال ليلا")
        assert hit["snippet"][hit["matchStart"]:hit["matchEnd"]] == "الاغتسالُ ليلاً"


class TestHighlighterWordBoundaries:
    TEXT = "والليلِ إذا يغشى، ثم الاغتسالُ ليلاً، وكذا اغتسالها بالليلِ. الإمامُ الصّادقُ"

    @pytest.mark.parametrize("query, expected", [
        ("الليل", None),                          # only inside والليل / بالليل
        ("الاغتسا", None),                        # a prefix of a word is not the word
        ("بالليل", "بالليلِ"),                     # trailing mark included
        ("الاغتسال ليلا", "الاغتسالُ ليلاً"),
        ("الامام الصادق", "الإمامُ الصّادقُ"),
    ])
    def test_matches_whole_words_only(self, query, expected):
        m = find_original_match(self.TEXT, normalize(query))
        assert (self.TEXT[m.start():m.end()] if m else None) == expected


class TestReindex:
    async def test_rebuilding_a_book_reproduces_the_importers_rows(self, imported: AsyncClient):
        texts = R.book_page_texts(imported.books_root, int(BOOK_ID))
        assert sorted(texts) == [1, 2, 3]
        async with get_sessionmaker()() as s:
            await s.execute(text(R.CREATE_TARGET_SQL))
            todo = [r[0] for r in (await s.execute(text(R.BOOKS_TO_BUILD_SQL))).all()]
            assert int(BOOK_ID) in todo
            await s.execute(text(R.INSERT_BOOK_SQL), R.book_params(int(BOOK_ID), texts))
            todo = [r[0] for r in (await s.execute(text(R.BOOKS_TO_BUILD_SQL))).all()]
            assert int(BOOK_ID) not in todo
            diff = await s.scalar(text(f"""
                SELECT count(*) FROM (
                    (SELECT * FROM pages WHERE book_id = :i EXCEPT SELECT * FROM {R.TARGET} WHERE book_id = :i)
                    UNION ALL
                    (SELECT * FROM {R.TARGET} WHERE book_id = :i EXCEPT SELECT * FROM pages WHERE book_id = :i)
                ) d
            """), {"i": int(BOOK_ID)})
            assert diff == 0
            await s.rollback()

    def test_a_page_missing_from_the_json_is_reported(self):
        assert R.missing_sequences([1, 2, 3, 4], {1: "a", 2: "b", 3: "c"}) == [4]
        assert R.missing_sequences([1, 2], {1: "a", 2: "b", 3: "c"}) == []

    def test_unreadable_json_is_none(self, tmp_path: Path):
        assert R.book_page_texts(tmp_path, 123) is None
