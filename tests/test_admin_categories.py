"""Admin page for reordering categories and moving them between the two sections."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.config import get_settings
from app.db import get_sessionmaker
from app.main import create_app


@pytest.fixture
async def admin():
    base = get_settings()
    async with get_sessionmaker()() as session:
        original = (await session.execute(
            text("SELECT id, sort_order, section FROM subjects ORDER BY sort_order")
        )).all()

    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        auth=(base.admin_username, base.admin_password),
    ) as client:
        yield client

    async with get_sessionmaker()() as session:
        for row in original:
            await session.execute(
                text("UPDATE subjects SET sort_order = :o, section = :s WHERE id = :i"),
                {"o": row.sort_order, "s": row.section, "i": row.id},
            )
        await session.commit()


async def order(client: AsyncClient) -> list[dict]:
    return (await client.get("/api/categories")).json()


def ids(cats: list[dict]) -> list[str]:
    return [c["id"] for c in cats]


async def move(client, subject_id, direction):
    response = await client.post("/admin/categories/move", data={"subject_id": subject_id, "direction": direction})
    assert response.status_code == 303
    return await order(client)


async def toggle(client, subject_id):
    response = await client.post("/admin/categories/section", data={"subject_id": subject_id})
    assert response.status_code == 303
    return await order(client)


def assert_well_formed(cats: list[dict]) -> None:
    assert [c["order"] for c in cats] == list(range(1, len(cats) + 1))
    sections = [c["section"] for c in cats]
    assert sections == sorted(sections, key=lambda s: s != "shia"), "sections must be two contiguous runs"


async def test_page_lists_every_category_in_order_under_both_sections(admin):
    page = (await admin.get("/admin/categories")).text
    cats = await order(admin)
    assert "الكتب الشيعية" in page and "الكتب الأخرى" in page
    positions = [page.index(c["title"]) for c in cats]
    assert positions == sorted(positions)


async def test_requires_admin_login(admin):
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as anon:
        assert (await anon.get("/admin/categories")).status_code == 401
        assert (await anon.post("/admin/categories/move", data={"subject_id": "الطب", "direction": "up"})).status_code == 401


async def test_move_down_swaps_with_the_next_and_the_api_follows(admin):
    before = await order(admin)
    after = await move(admin, before[1]["id"], "down")
    assert ids(after)[:4] == [before[0]["id"], before[2]["id"], before[1]["id"], before[3]["id"]]
    assert_well_formed(after)


async def test_move_up_swaps_with_the_previous(admin):
    before = await order(admin)
    after = await move(admin, before[5]["id"], "up")
    assert ids(after)[4:6] == [before[5]["id"], before[4]["id"]]
    assert_well_formed(after)


async def test_moving_past_either_end_changes_nothing(admin):
    before = await order(admin)
    assert await move(admin, before[0]["id"], "up") == before
    assert await move(admin, before[-1]["id"], "down") == before


async def test_crossing_the_boundary_joins_the_other_section_and_the_boundary_stays(admin):
    before = await order(admin)
    boundary = sum(c["section"] == "shia" for c in before)
    first_other, last_shia = before[boundary], before[boundary - 1]
    after = await move(admin, first_other["id"], "up")

    by_id = {c["id"]: c for c in after}
    assert by_id[first_other["id"]]["section"] == "shia"
    assert by_id[last_shia["id"]]["section"] == "other"
    assert sum(c["section"] == "shia" for c in after) == boundary
    assert_well_formed(after)


async def test_moving_a_shia_category_to_the_other_section_puts_it_first_there(admin):
    before = await order(admin)
    boundary = sum(c["section"] == "shia" for c in before)
    target = before[4]
    after = await toggle(admin, target["id"])

    assert after[boundary - 1]["id"] == target["id"] and after[boundary - 1]["section"] == "other"
    assert sum(c["section"] == "shia" for c in after) == boundary - 1
    assert ids(after)[:4] == ids(before)[:4]
    assert_well_formed(after)


async def test_moving_an_other_category_to_shia_puts_it_last_there(admin):
    before = await order(admin)
    boundary = sum(c["section"] == "shia" for c in before)
    target = before[boundary + 3]
    after = await toggle(admin, target["id"])

    assert after[boundary]["id"] == target["id"] and after[boundary]["section"] == "shia"
    assert sum(c["section"] == "shia" for c in after) == boundary + 1
    assert_well_formed(after)


async def test_toggling_twice_returns_to_the_same_section_at_the_boundary(admin):
    before = await order(admin)
    target = before[2]
    once = await toggle(admin, target["id"])
    twice = await toggle(admin, target["id"])
    assert next(c for c in twice if c["id"] == target["id"])["section"] == "shia"
    assert sum(c["section"] == "shia" for c in twice) == sum(c["section"] == "shia" for c in before)
    assert_well_formed(twice)
    assert ids(once) != ids(before)


async def test_a_change_moves_the_category_list_version_and_nothing_else_moves(admin):
    v0 = (await admin.get("/api/catalog/version")).json()
    cats = await order(admin)
    await move(admin, cats[2]["id"], "down")
    v1 = (await admin.get("/api/catalog/version")).json()
    assert v1["categories"]["version"] != v0["categories"]["version"]
    assert v1["books"] == v0["books"]  # no book carries its category's position


async def test_a_no_op_does_not_move_the_version(admin):
    cats = await order(admin)
    v0 = (await admin.get("/api/catalog/version")).json()["categories"]["version"]
    await move(admin, cats[0]["id"], "up")
    assert (await admin.get("/api/catalog/version")).json()["categories"]["version"] == v0


async def test_unknown_category_is_a_404(admin):
    assert (await admin.post("/admin/categories/move", data={"subject_id": "لا يوجد", "direction": "up"})).status_code == 404
    assert (await admin.post("/admin/categories/section", data={"subject_id": "لا يوجد"})).status_code == 404


async def test_pins_survive_a_reorder(admin, tmp_path):
    import importlib.util
    import sys
    from pathlib import Path

    importer_path = Path(__file__).resolve().parents[1] / "scripts" / "import" / "import_books.py"
    spec = importlib.util.spec_from_file_location("import_books", importer_path)
    import_books = importlib.util.module_from_spec(spec)
    sys.modules["import_books"] = import_books
    spec.loader.exec_module(import_books)

    book_id = 8887001
    (tmp_path / f"{book_id}.abx").write_text(
        "checksum\n< اسم الكتاب > كتاب اختبار التثبيت < / اسم الكتاب >\n"
        "< اسم المؤلف > مؤلف التثبيت < / اسم المؤلف >\n< الكتاب >\n< صفحة > 1 < / صفحة >\nنص.\n< / الكتاب >\n",
        encoding="utf-8",
    )
    async with get_sessionmaker()() as session:
        assert await import_books.import_one(
            session, tmp_path / f"{book_id}.abx", "test", tmp_path / "books", False
        ) == "ok"
        await session.execute(
            text("INSERT INTO subject_pinned_books (subject_id, book_id, position) VALUES ('الطب', :b, 1)"),
            {"b": book_id},
        )
        await session.commit()
    try:
        before = next(c for c in await order(admin) if c["id"] == "الطب")
        assert before["pinnedBookIds"] == [str(book_id)]
        after = await move(admin, "الطب", "up")
        assert next(c for c in after if c["id"] == "الطب")["pinnedBookIds"] == [str(book_id)]
    finally:
        async with get_sessionmaker()() as session:
            await session.execute(text("DELETE FROM books WHERE id = :b"), {"b": book_id})
            await session.execute(text("DELETE FROM works WHERE title_norm = 'كتاب اختبار التثبيت'"))
            await session.execute(text("DELETE FROM authors WHERE name_norm = 'مؤلف التثبيت'"))
            await session.commit()
