"""Admin JSON upload: a new book, a second volume added through its work, and the guards
around volumes -- run against the real importer and database, like the API tests."""

import json
from pathlib import Path
from urllib.parse import unquote

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.config import get_settings
from app.db import get_sessionmaker
from app.main import create_app

TITLE = "كتاب اختبار الرفع المتعدد"
AUTHOR = "مؤلف اختبار الرفع"


def book_json(title=TITLE, author=AUTHOR, book_id="900001", volume=None, marker="أ") -> bytes:
    metadata = {"volume": str(volume)} if volume else {}
    content = {
        "schemaVersion": 2, "bookId": book_id, "title": title, "author": author,
        "metadata": metadata,
        "pages": [{
            "id": "p-000001", "sequence": 1, "isBlank": False, "pageType": "main",
            "pageNumber": "1", "printedPage": 1, "sourcePageLabel": "1",
            "blocks": [{"id": "p-000001-b-001", "type": "text", "order": 1,
                        "text": f"نص تجريبي للمجلد {marker}"}],
        }],
        "toc": [],
    }
    return json.dumps(content, ensure_ascii=False).encode("utf-8")


@pytest.fixture
async def admin(tmp_path: Path, monkeypatch):
    base = get_settings()
    patched = base.model_copy(update={"books_root": tmp_path / "books"})
    monkeypatch.setattr("app.api.admin.get_settings", lambda: patched)

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        auth=(base.admin_username, base.admin_password),
    ) as client:
        yield client

    async with get_sessionmaker()() as session:
        await session.execute(text("DELETE FROM books WHERE title_norm = 'كتاب اختبار الرفع المتعدد'"))
        await session.execute(text("DELETE FROM works WHERE title_norm = 'كتاب اختبار الرفع المتعدد'"))
        await session.execute(text("DELETE FROM authors WHERE name_norm = 'مؤلف اختبار الرفع'"))
        await session.commit()


async def upload(client, data: bytes, **form):
    return await client.post(
        "/admin/books/new/json", data=form,
        files={"file": ("book.json", data, "application/json")},
    )


def redirect_target(response) -> str:
    assert response.status_code == 303, response.text
    return unquote(response.headers["location"])


async def test_upload_creates_book_and_ignores_the_files_own_id(admin):
    response = await upload(admin, book_json(book_id="1"))
    target = redirect_target(response)
    assert "?ok=" in target and target.startswith("/admin/works/")

    async with get_sessionmaker()() as session:
        row = (await session.execute(
            text("SELECT id, is_published, page_count FROM books WHERE title_norm = :t"),
            {"t": "كتاب اختبار الرفع المتعدد"},
        )).one()
    assert row.id >= 900_000  # a fresh id, never the file's "1"
    assert row.is_published and row.page_count == 1

    api = (await admin.get(f"/api/books/{row.id}")).json()
    assert api["title"] == TITLE and api["author"] == AUTHOR


async def test_second_volume_joins_the_same_work(admin):
    first = redirect_target(await upload(admin, book_json(volume=1)))
    work_id = first.split("/admin/works/")[1].split("?")[0]

    second = await upload(admin, book_json(title="عنوان مختلف", author="آخر", marker="ب"),
                          work_id=work_id, volume="2")
    assert redirect_target(second).startswith(f"/admin/works/{work_id}?ok=")

    async with get_sessionmaker()() as session:
        rows = (await session.execute(
            text("SELECT b.volume, b.title, b.work_id FROM books b WHERE b.work_id = :w ORDER BY b.volume"),
            {"w": int(work_id)},
        )).all()
    assert [r.volume for r in rows] == [1, 2]
    assert {r.title for r in rows} == {TITLE}  # title/author forced from the work


async def test_duplicate_volume_is_refused(admin):
    first = redirect_target(await upload(admin, book_json(volume=1)))
    work_id = first.split("/admin/works/")[1].split("?")[0]
    again = redirect_target(await upload(admin, book_json(volume=1), work_id=work_id, volume="1"))
    assert "err=" in again and "المجلد 1 موجود مسبقاً" in again


async def test_same_title_without_volume_number_is_refused(admin):
    await upload(admin, book_json(volume=1))
    target = redirect_target(await upload(admin, book_json()))
    assert "err=" in target and "حدّد رقم المجلد" in target


async def test_add_volume_requires_a_volume_number(admin):
    first = redirect_target(await upload(admin, book_json(volume=1)))
    work_id = first.split("/admin/works/")[1].split("?")[0]
    target = redirect_target(await upload(admin, book_json(), work_id=work_id))
    assert "رقم المجلد مطلوب" in target


async def test_invalid_files_are_rejected_without_importing(admin):
    bad_json = redirect_target(await upload(admin, b"{not json"))
    assert "ليس JSON صالحاً" in bad_json

    broken = json.loads(book_json())
    broken["pages"][0]["sequence"] = 5  # breaks the page-sequence invariant
    invalid = redirect_target(await upload(admin, json.dumps(broken, ensure_ascii=False).encode()))
    assert "الملف غير صالح" in invalid

    async with get_sessionmaker()() as session:
        count = await session.scalar(
            text("SELECT count(*) FROM books WHERE title_norm = 'كتاب اختبار الرفع المتعدد'")
        )
    assert count == 0


async def test_admin_pages_render_the_new_forms(admin):
    new_page = (await admin.get("/admin/books/new")).text
    assert "/admin/books/new/json" in new_page and "متعدد المجلدات" in new_page

    work_id = redirect_target(await upload(admin, book_json(volume=1))).split("/admin/works/")[1].split("?")[0]
    work_page = (await admin.get(f"/admin/works/{work_id}")).text
    assert 'name="work_id"' in work_page and 'name="volume"' in work_page and 'value="2"' in work_page


async def test_work_page_shows_its_work_id(admin):
    target = redirect_target(await upload(admin, book_json(volume=1)))
    work_id = target.split("/admin/works/")[1].split("?")[0]
    page = await admin.get(f"/admin/works/{work_id}")
    assert f"رقم العمل: <strong>{work_id}</strong>" in page.text
