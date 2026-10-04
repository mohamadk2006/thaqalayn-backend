"""sort=work and sort=oldest: results by the book, not by occurrences."""

import importlib.util
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.db import get_sessionmaker
from app.main import create_app
from app.services import search_service
from app.services.arabic import normalize

IMPORTER = Path(__file__).resolve().parents[1] / "scripts" / "import" / "import_books.py"
spec = importlib.util.spec_from_file_location("import_books", IMPORTER)
import_books = importlib.util.module_from_spec(spec)
sys.modules["import_books"] = import_books
spec.loader.exec_module(import_books)

# (book id, title, author, hijri death year or None, number of times the phrase is on page 2)
BOOKS = [
    (8885101, "باء كتاب ترتيب النتائج", "مؤلف ترتيب الأول", 400, 1),
    (8885102, "ألف كتاب ترتيب النتائج", "مؤلف ترتيب الثاني", 200, 3),
    (8885103, "جيم كتاب ترتيب النتائج", "مؤلف ترتيب الثالث", None, 5),
]
PHRASE = "عبارة الترتيب الفريدة"


def source(title: str, author: str, repeats: int) -> str:
    return f"""checksum
< اسم الكتاب > {title} < / اسم الكتاب >
< اسم المؤلف > {author} < / اسم المؤلف >
< الكتاب >
< صفحة > 1 < / صفحة >
{PHRASE} هنا مرة.
< صفحة > 2 < / صفحة >
{(PHRASE + " ") * repeats}
< / الكتاب >
"""


@pytest.fixture
async def client(tmp_path: Path):
    search_service._order_cache.clear()
    books_root = tmp_path / "books"
    async with get_sessionmaker()() as session:
        for book_id, title, author, year, repeats in BOOKS:
            (tmp_path / f"{book_id}.abx").write_text(source(title, author, repeats), encoding="utf-8")
            assert await import_books.import_one(
                session, tmp_path / f"{book_id}.abx", "test", books_root, False) == "ok"
            await session.execute(text("UPDATE authors SET death_year_hijri = :y, death_label = :l "
                                       "WHERE name_norm = :n"),
                                  {"y": year, "l": "معاصر" if year is None else f"{year} هـ",
                                   "n": normalize(author)})
        await session.commit()

    from app.config import Settings, get_settings

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(update={"books_root": books_root})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c

    search_service._order_cache.clear()
    async with get_sessionmaker()() as session:
        for book_id, title, author, *_ in BOOKS:
            await session.execute(text("DELETE FROM books WHERE id = :i"), {"i": book_id})
            await session.execute(text("DELETE FROM works WHERE title_norm = :t"), {"t": normalize(title)})
            await session.execute(text("DELETE FROM authors WHERE name_norm = :n"), {"n": normalize(author)})
        await session.commit()


async def hits(client, **params):
    r = await client.get("/api/search", params={"q": PHRASE, "limit": 50, **params})
    assert r.status_code == 200, r.text
    body = r.json()
    return [(h["workTitle"].split()[0], h["pageSequence"]) for h in body["items"]], body["total"]


async def test_oldest_author_first_then_work_and_page(client):
    found, total = await hits(client, sort="oldest")
    # 200, 400, then the author with no death year last; pages in book order
    assert found == [("ألف", 1), ("ألف", 2), ("باء", 1), ("باء", 2), ("جيم", 1), ("جيم", 2)]
    assert total == 6


async def test_by_work_is_alphabetical_by_title(client):
    found, _ = await hits(client, sort="work")
    assert [t for t, _ in found] == ["ألف", "ألف", "باء", "باء", "جيم", "جيم"]


async def test_default_order_is_oldest_first(client):
    default, _ = await hits(client)
    assert default == (await hits(client, sort="oldest"))[0]
    assert default[0] == ("ألف", 1)


async def test_relevance_is_by_occurrences(client):
    found, _ = await hits(client, sort="relevance")
    assert found[0] == ("جيم", 2)  # five occurrences on one page


async def test_paging_through_an_ordered_search(client):
    first, _ = await hits(client, sort="oldest", limit=4)
    r = await client.get("/api/search", params={"q": PHRASE, "limit": 4, "page": 2, "sort": "oldest"})
    second = [(h["workTitle"].split()[0], h["pageSequence"]) for h in r.json()["items"]]
    assert first + second == [("ألف", 1), ("ألف", 2), ("باء", 1), ("باء", 2), ("جيم", 1), ("جيم", 2)]


async def test_an_unknown_sort_is_refused(client):
    r = await client.get("/api/search", params={"q": PHRASE, "sort": "newest"})
    assert r.status_code == 422
