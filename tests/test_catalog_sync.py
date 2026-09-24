"""GET /api/catalog/version and GET /api/books/changes, against real books imported through
the actual pipeline and the real database triggers."""

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

IDS = (8886001, 8886002, 8886003)
A, B = "الطب", "الفرق والمذاهب"
LIB_TITLE = "مكتبة اختبار المزامنة"
AUTHOR = "مؤلف اختبار المزامنة"


def source(n: int) -> str:
    return f"""checksum
< اسم الكتاب > كتاب اختبار المزامنة {n} < / اسم الكتاب >
< اسم المؤلف > {AUTHOR} < / اسم المؤلف >
< الكتاب >
< صفحة > 1 < / صفحة >
نص تجريبي رقم {n}.
< / الكتاب >
"""


@pytest.fixture
async def sync(tmp_path: Path):
    async with get_sessionmaker()() as session:
        for i, book_id in enumerate(IDS, start=1):
            (tmp_path / f"{book_id}.abx").write_text(source(i), encoding="utf-8")
            assert await import_books.import_one(
                session, tmp_path / f"{book_id}.abx", "test", tmp_path / "books", False
            ) == "ok"
        await session.commit()

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    async with get_sessionmaker()() as session:
        await session.execute(text("UPDATE catalog_meta SET value = 0 WHERE key = 'tombstone_floor'"))
        await session.execute(text("DELETE FROM books WHERE id = ANY(:ids)"), {"ids": list(IDS)})
        await session.execute(text("DELETE FROM works WHERE title_norm LIKE 'كتاب اختبار المزامنه%'"))
        await session.execute(text("DELETE FROM authors WHERE name_norm = 'مؤلف اختبار المزامنه'"))
        await session.execute(text("DELETE FROM libraries WHERE title = :t"), {"t": LIB_TITLE})
        await session.commit()


async def versions(client: AsyncClient) -> dict:
    response = await client.get("/api/catalog/version")
    assert response.status_code == 200
    return response.json()


async def changes(client: AsyncClient, since, **params) -> dict:
    return (await client.get("/api/books/changes", params={"since": since, **params})).json()


async def drain(client: AsyncClient, since, limit=500) -> list[dict]:
    """Every change after `since`, following hasMore like the app does."""
    out = []
    while True:
        body = await changes(client, since, limit=limit)
        assert body["reset"] is False
        out += body["items"]
        since = body["cursor"]
        if not body["hasMore"]:
            return out


async def work_of(book_id: int) -> int:
    async with get_sessionmaker()() as session:
        return await session.scalar(text("SELECT work_id FROM books WHERE id = :i"), {"i": book_id})


async def sql(statement: str, **params):
    async with get_sessionmaker()() as session:
        await session.execute(text(statement), params)
        await session.commit()


def mine(items: list[dict]) -> list[dict]:
    return [i for i in items if i.get("bookId") in map(str, IDS) or i.get("book", {}).get("bookId") in map(str, IDS)]


class TestVersionEndpoint:
    async def test_unchanged_catalog_gives_identical_bodies_and_a_304(self, sync: AsyncClient):
        first = await sync.get("/api/catalog/version")
        second = await sync.get("/api/catalog/version")
        assert first.json() == second.json()
        etag = first.headers["etag"]
        cached = await sync.get("/api/catalog/version", headers={"If-None-Match": etag})
        assert cached.status_code == 304 and cached.content == b""

    async def test_lists_every_category_and_library_with_a_count(self, sync: AsyncClient):
        v = await versions(sync)
        cats = (await sync.get("/api/categories")).json()
        assert set(v["categories"]["items"]) == {c["id"] for c in cats}
        libs = (await sync.get("/api/libraries")).json()
        assert set(v["libraries"]["items"]) == {lib["id"] for lib in libs}
        assert set(v) == {"categories", "libraries", "books", "featuredWorks"}
        assert v["books"]["cursor"].isdigit()

    async def test_count_matches_the_books_endpoint(self, sync: AsyncClient):
        work = await work_of(IDS[0])
        await sql("INSERT INTO work_subjects (work_id, subject_id) VALUES (:w, :s)", w=work, s=A)
        v = await versions(sync)
        total = (await sync.get("/api/books", params={"subject": A, "limit": 1})).json()["total"]
        assert v["categories"]["items"][A]["count"] == total

    async def test_categories_carry_their_position(self, sync: AsyncClient):
        cats = (await sync.get("/api/categories")).json()
        assert all(isinstance(c["order"], int) for c in cats)
        assert [c["order"] for c in cats] == sorted(c["order"] for c in cats)


class TestAddingAndMovingBooks:
    async def test_adding_a_book_to_a_category(self, sync: AsyncClient):
        before = await versions(sync)
        work = await work_of(IDS[0])
        await sql("INSERT INTO work_subjects (work_id, subject_id) VALUES (:w, :s)", w=work, s=A)
        after = await versions(sync)

        assert after["categories"]["items"][A]["version"] != before["categories"]["items"][A]["version"]
        assert after["categories"]["items"][A]["count"] == before["categories"]["items"][A]["count"] + 1
        assert after["books"]["cursor"] != before["books"]["cursor"]
        for other in before["categories"]["items"]:
            if other != A:
                assert after["categories"]["items"][other] == before["categories"]["items"][other], other

        items = mine(await drain(sync, before["books"]["cursor"]))
        assert [i["op"] for i in items] == ["upsert"]
        assert items[0]["book"]["bookId"] == str(IDS[0])
        assert A in [s["id"] for s in items[0]["book"]["subjects"]]

    async def test_moving_a_book_between_categories(self, sync: AsyncClient):
        work = await work_of(IDS[0])
        await sql("INSERT INTO work_subjects (work_id, subject_id) VALUES (:w, :s)", w=work, s=A)
        before = await versions(sync)
        await sql("DELETE FROM work_subjects WHERE work_id = :w AND subject_id = :a", w=work, a=A)
        await sql("INSERT INTO work_subjects (work_id, subject_id) VALUES (:w, :s)", w=work, s=B)
        after = await versions(sync)

        for cat in (A, B):
            assert after["categories"]["items"][cat]["version"] != before["categories"]["items"][cat]["version"]
        assert after["categories"]["items"][A]["count"] == before["categories"]["items"][A]["count"] - 1
        assert after["categories"]["items"][B]["count"] == before["categories"]["items"][B]["count"] + 1

        final = [i for i in mine(await drain(sync, before["books"]["cursor"])) if i["op"] == "upsert"][-1]
        assert [s["id"] for s in final["book"]["subjects"]] == [B]

    async def test_unrelated_categories_do_not_move(self, sync: AsyncClient):
        work = await work_of(IDS[0])
        before = await versions(sync)
        await sql("INSERT INTO work_subjects (work_id, subject_id) VALUES (:w, :s)", w=work, s=A)
        after = await versions(sync)
        changed = [c for c in before["categories"]["items"]
                   if after["categories"]["items"][c] != before["categories"]["items"][c]]
        assert changed == [A]

    async def test_editing_a_book_field_moves_its_category_and_the_feed(self, sync: AsyncClient):
        work = await work_of(IDS[0])
        await sql("INSERT INTO work_subjects (work_id, subject_id) VALUES (:w, :s)", w=work, s=A)
        before = await versions(sync)
        await sql("UPDATE books SET publisher = 'دار المزامنة' WHERE id = :i", i=IDS[0])
        after = await versions(sync)
        assert after["categories"]["items"][A]["version"] != before["categories"]["items"][A]["version"]
        assert after["categories"]["items"][A]["count"] == before["categories"]["items"][A]["count"]
        item = mine(await drain(sync, before["books"]["cursor"]))[0]
        assert item["book"]["publisher"] == "دار المزامنة"

    async def test_a_reimport_that_changes_nothing_is_not_a_change(self, sync: AsyncClient):
        before = await versions(sync)
        await sql("UPDATE books SET updated_at = now(), imported_at = now() WHERE id = :i", i=IDS[0])
        assert await versions(sync) == before


class TestRenamesAndRemovals:
    async def test_renaming_a_category_moves_its_books(self, sync: AsyncClient):
        work = await work_of(IDS[0])
        await sql("INSERT INTO work_subjects (work_id, subject_id) VALUES (:w, :s)", w=work, s=A)
        before = await versions(sync)
        try:
            await sql("UPDATE subjects SET title = 'الطب (اسم جديد)' WHERE id = :s", s=A)
            after = await versions(sync)
            assert after["categories"]["version"] != before["categories"]["version"]
            assert after["categories"]["items"][A]["version"] != before["categories"]["items"][A]["version"]
            book = mine(await drain(sync, before["books"]["cursor"]))[0]["book"]
            assert [s["title"] for s in book["subjects"]] == ["الطب (اسم جديد)"]
        finally:
            await sql("UPDATE subjects SET title = :s WHERE id = :s", s=A)

    async def test_reordering_categories_changes_only_the_category_list(self, sync: AsyncClient):
        before = await versions(sync)
        try:
            await sql("UPDATE subjects SET sort_order = sort_order + 1000 WHERE id = :s", s=A)
            after = await versions(sync)
            assert after["categories"]["version"] != before["categories"]["version"]
            assert after["books"] == before["books"]
        finally:
            await sql("UPDATE subjects SET sort_order = sort_order - 1000 WHERE id = :s", s=A)

    async def test_deleting_a_book(self, sync: AsyncClient):
        work = await work_of(IDS[0])
        await sql("INSERT INTO work_subjects (work_id, subject_id) VALUES (:w, :s)", w=work, s=A)
        before = await versions(sync)
        await sql("DELETE FROM books WHERE id = :i", i=IDS[0])
        after = await versions(sync)

        assert after["categories"]["items"][A]["version"] != before["categories"]["items"][A]["version"]
        assert after["categories"]["items"][A]["count"] == before["categories"]["items"][A]["count"] - 1
        items = mine(await drain(sync, before["books"]["cursor"]))
        assert items[-1] == {"seq": items[-1]["seq"], "op": "delete", "bookId": str(IDS[0])}

    async def test_unpublishing_a_book_is_a_delete_and_republishing_an_upsert(self, sync: AsyncClient):
        before = await versions(sync)
        await sql("UPDATE books SET is_published = false WHERE id = :i", i=IDS[1])
        mid = await versions(sync)
        assert mine(await drain(sync, before["books"]["cursor"]))[-1]["op"] == "delete"
        await sql("UPDATE books SET is_published = true WHERE id = :i", i=IDS[1])
        assert mine(await drain(sync, mid["books"]["cursor"]))[-1]["op"] == "upsert"


class TestLibraries:
    async def test_adding_a_book_to_a_library(self, sync: AsyncClient):
        async with get_sessionmaker()() as session:
            lib = await session.scalar(text("INSERT INTO libraries (title) VALUES (:t) RETURNING id"), {"t": LIB_TITLE})
            await session.commit()
        work = await work_of(IDS[0])
        before = await versions(sync)
        assert str(lib) in before["libraries"]["items"]
        await sql("INSERT INTO library_works (library_id, work_id) VALUES (:l, :w)", l=lib, w=work)
        after = await versions(sync)
        assert after["libraries"]["items"][str(lib)]["version"] != before["libraries"]["items"][str(lib)]["version"]
        assert after["libraries"]["items"][str(lib)]["count"] == 1
        book = mine(await drain(sync, before["books"]["cursor"]))[0]["book"]
        assert [lb["title"] for lb in book["libraries"]] == [LIB_TITLE]

    async def test_creating_a_library_changes_the_library_list_version(self, sync: AsyncClient):
        before = await versions(sync)
        await sql("INSERT INTO libraries (title) VALUES (:t)", t=LIB_TITLE)
        assert (await versions(sync))["libraries"]["version"] != before["libraries"]["version"]


class TestFeedRules:
    async def test_reset_when_the_cursor_is_unusable(self, sync: AsyncClient):
        current = (await versions(sync))["books"]["cursor"]
        for bad in ("0", "abc", "-5", str(int(current) + 10_000_000)):
            body = await changes(sync, bad)
            assert body == {"cursor": current, "hasMore": False, "reset": True, "items": []}, bad
        missing = (await sync.get("/api/books/changes")).json()
        assert missing["reset"] is True

    async def test_reset_when_the_cursor_is_older_than_what_is_remembered(self, sync: AsyncClient):
        old = (await versions(sync))["books"]["cursor"]
        await sql("UPDATE books SET publisher = 'x' WHERE id = :i", i=IDS[0])
        current = int((await versions(sync))["books"]["cursor"])
        await sql("UPDATE catalog_meta SET value = :v WHERE key = 'tombstone_floor'", v=current)
        assert (await changes(sync, old))["reset"] is True
        assert (await changes(sync, str(current)))["reset"] is False

    async def test_no_changes_returns_the_same_cursor(self, sync: AsyncClient):
        current = (await versions(sync))["books"]["cursor"]
        assert await changes(sync, current) == {"cursor": current, "hasMore": False, "reset": False, "items": []}

    async def test_paging_has_no_gaps_or_repeats(self, sync: AsyncClient):
        start = (await versions(sync))["books"]["cursor"]
        for book_id in IDS:
            await sql("UPDATE books SET publisher = 'صفحة' WHERE id = :i", i=book_id)

        seen, since, flags = [], start, []
        while True:
            body = await changes(sync, since, limit=1)
            flags.append(body["hasMore"])
            seen += [i["book"]["bookId"] for i in body["items"]]
            since = body["cursor"]
            if not body["hasMore"]:
                break
        assert flags == [True, True, False]
        assert sorted(seen) == sorted(str(i) for i in IDS)
        assert since == (await versions(sync))["books"]["cursor"]

    async def test_upserts_have_exactly_the_shape_of_the_books_endpoint(self, sync: AsyncClient):
        start = (await versions(sync))["books"]["cursor"]
        await sql("UPDATE books SET publisher = 'شكل' WHERE id = :i", i=IDS[0])
        item = mine(await drain(sync, start))[0]
        assert item["book"] == (await sync.get(f"/api/books/{IDS[0]}")).json()

    async def test_seq_only_increases(self, sync: AsyncClient):
        start = (await versions(sync))["books"]["cursor"]
        for book_id in IDS:
            await sql("UPDATE books SET publisher = 'ترتيب' WHERE id = :i", i=book_id)
        seqs = [i["seq"] for i in await drain(sync, start)]
        assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))


class TestUncommittedChangesAreNotServed:
    async def test_a_change_still_being_written_is_invisible_until_it_commits(self, sync: AsyncClient):
        """Sequence numbers are handed out before commit. If the feed showed a later, already
        committed change while an earlier one was still open, a client would move past the
        open one and never see it."""
        start = (await versions(sync))["books"]["cursor"]
        async with get_sessionmaker()() as slow:
            await slow.execute(text("UPDATE books SET publisher = 'قيد الكتابة' WHERE id = :i"), {"i": IDS[0]})
            # a faster transaction, started later, commits first
            await sql("UPDATE books SET publisher = 'أسرع' WHERE id = :i", i=IDS[1])

            during = await drain(sync, start)
            assert mine(during) == []  # nothing may be served while the earlier one is open
            assert (await versions(sync))["books"]["cursor"] == start

            await slow.commit()

        after = mine(await drain(sync, start))
        assert sorted(i["book"]["bookId"] for i in after) == [str(IDS[0]), str(IDS[1])]
