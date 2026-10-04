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
    search_service._order_cache = None
    books_root = tmp_path / "books"
    async with get_sessionmaker()() as session:
        for book_id, title, author, year, repeats in BOOKS:
            (tmp_path / f"{book_id}.abx").write_text(source(title, author, repeats), encoding="utf-8")
            assert await import_books.import_one(
                session, tmp_path / f"{book_id}.abx", "test", books_root, False) == "ok"
            label = "معاصر" if year is None else f"{year} هـ"
            await session.execute(
                text("UPDATE authors SET death_year_hijri = :y, death_label = :l WHERE name_norm = :n"),
                {"y": year, "l": label, "n": normalize(author)})
        await session.commit()

    from app.config import get_settings

    app = create_app()
    app.dependency_overrides[get_settings] = (
        lambda: get_settings().model_copy(update={"books_root": books_root}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c

    search_service._order_cache = None
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
    found = [(h["workTitle"].split()[0], h["pageSequence"]) for h in body["items"]]
    return found, body["total"]


async def test_oldest_author_first_then_work_volume_and_page(client):
    found, total = await hits(client)
    # death years 200, 400, then the author with none last; pages in book order, whatever
    # the number of occurrences (the last book has the most)
    expected = [("ألف", 1), ("ألف", 2), ("باء", 1), ("باء", 2), ("جيم", 1), ("جيم", 2)]
    assert found == expected
    assert total == 6


async def test_paging_keeps_the_order(client):
    first, _ = await hits(client, limit=4)
    r = await client.get("/api/search", params={"q": PHRASE, "limit": 4, "page": 2})
    second = [(h["workTitle"].split()[0], h["pageSequence"]) for h in r.json()["items"]]
    expected = [("ألف", 1), ("ألف", 2), ("باء", 1), ("باء", 2), ("جيم", 1), ("جيم", 2)]
    assert first + second == expected
