"""End-to-end importer test: a synthetic .abx through convert → validate → paginate →
database, asserting the rows and idempotency behaviour the ~18,800-file run depends on.

Uses a book id far outside the real range so it can't collide with imported data, and
cleans up after itself.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from app.db import get_sessionmaker

IMPORTER = Path(__file__).resolve().parents[1] / "scripts" / "import" / "import_books.py"
spec = importlib.util.spec_from_file_location("import_books", IMPORTER)
import_books = importlib.util.module_from_spec(spec)
sys.modules["import_books"] = import_books
spec.loader.exec_module(import_books)

TEST_ID = "8880001"
ABX = """checksum
< اسم الكتاب > كتاب الاختبار < / اسم الكتاب >
< اسم المؤلف > المؤلف التجريبي < / اسم المؤلف >
< جزء > 2 < / جزء >
< مجموعة > مصادر التفسير عند الشيعة < / مجموعة >
< الناشر > دار الاختبار < / الناشر >
< سنة الوفاة > 460 < / سنة الوفاة >
< الكتاب >
< صفحة > 1 < / صفحة >
< فهرس الموضوعات >
الباب الأول
< / فهرس الموضوعات >
قال الإمام الصادق عليه السلام في هذا الموضع.
نص إضافي على نفس الصفحة.
< صفحة > 2 < / صفحة >
< فهرس الموضوعات >
الباب الثاني
< / فهرس الموضوعات >
نص في الصفحة الثانية.
< / الكتاب >
"""


@pytest.fixture
async def cleanup():
    yield
    async with get_sessionmaker()() as s:
        await s.execute(text("DELETE FROM books WHERE id = :i"), {"i": int(TEST_ID)})
        await s.execute(text("DELETE FROM import_log WHERE book_id = :i"), {"i": int(TEST_ID)})
        await s.execute(text(
            "DELETE FROM works WHERE title_norm = 'كتاب الاختبار'"))
        await s.execute(text(
            "DELETE FROM authors WHERE name_norm = 'المؤلف التجريبي'"))
        await s.commit()


async def _run_import(source_dir: Path, books_root: Path, force: bool = False) -> str:
    run_id = "test-run"
    async with get_sessionmaker()() as session:
        return await import_books.import_one(
            session, source_dir / f"{TEST_ID}.abx", run_id, books_root, force
        )


async def test_full_pipeline_imports_book_work_author_pages_sections(
    tmp_path: Path, cleanup
):
    (tmp_path / f"{TEST_ID}.abx").write_text(ABX, encoding="utf-8")
    result = await _run_import(tmp_path, tmp_path / "books")
    assert result == "ok"

    async with get_sessionmaker()() as s:
        row = (await s.execute(text("""
            SELECT b.volume, b.language_code, b.publisher, b.page_count,
                   w.title AS work_title, w.subject_id,
                   a.name AS author, a.death_year_hijri
            FROM books b JOIN works w ON w.id=b.work_id
            LEFT JOIN authors a ON a.id=b.author_id
            WHERE b.id=:i"""), {"i": int(TEST_ID)})).one()
    volume, lang, publisher, page_count, work_title, subject, author, death = row
    assert volume == 2
    assert lang == "ar"
    assert publisher == "دار الاختبار"
    assert page_count == 2
    assert work_title == "كتاب الاختبار"  # clean, no volume suffix
    assert subject == "tafsir-shia"
    assert author == "المؤلف التجريبي"
    assert death == 460


async def test_file_written_to_books_root(tmp_path: Path, cleanup):
    (tmp_path / f"{TEST_ID}.abx").write_text(ABX, encoding="utf-8")
    await _run_import(tmp_path, tmp_path / "books")
    written = tmp_path / "books" / f"{TEST_ID}.json"
    assert written.exists()
    # Compact, not pretty-printed.
    assert b"\n  " not in written.read_bytes()


async def test_second_run_skips_unchanged(tmp_path: Path, cleanup):
    (tmp_path / f"{TEST_ID}.abx").write_text(ABX, encoding="utf-8")
    assert await _run_import(tmp_path, tmp_path / "books") == "ok"
    assert await _run_import(tmp_path, tmp_path / "books") == "skipped"


async def test_changed_source_reimports_and_bumps_version(tmp_path: Path, cleanup):
    (tmp_path / f"{TEST_ID}.abx").write_text(ABX, encoding="utf-8")
    await _run_import(tmp_path, tmp_path / "books")

    async with get_sessionmaker()() as s:
        v1 = await s.scalar(
            text("SELECT content_version FROM books WHERE id=:i"), {"i": int(TEST_ID)}
        )

    (tmp_path / f"{TEST_ID}.abx").write_text(
        ABX.replace("نص في الصفحة الثانية.", "نص معدّل في الصفحة الثانية."), encoding="utf-8"
    )
    assert await _run_import(tmp_path, tmp_path / "books") == "ok"

    async with get_sessionmaker()() as s:
        v2 = await s.scalar(
            text("SELECT content_version FROM books WHERE id=:i"), {"i": int(TEST_ID)}
        )
    assert v2 == v1 + 1


async def test_malformed_source_is_logged_not_raised(tmp_path: Path, cleanup):
    (tmp_path / f"{TEST_ID}.abx").write_text(
        "checksum\n< no body marker here >\n", encoding="utf-8"
    )
    result = await _run_import(tmp_path, tmp_path / "books")
    assert result == "failed"

    async with get_sessionmaker()() as s:
        status = await s.scalar(
            text("SELECT status FROM import_log WHERE book_id=:i ORDER BY id DESC LIMIT 1"),
            {"i": int(TEST_ID)},
        )
    assert status == "failed"


async def test_search_finds_imported_content(tmp_path: Path, cleanup):
    """The milestone's real finish line: imported content is searchable, undiacriticized."""
    (tmp_path / f"{TEST_ID}.abx").write_text(ABX, encoding="utf-8")
    await _run_import(tmp_path, tmp_path / "books")

    async with get_sessionmaker()() as s:
        hit = (await s.execute(text("""
            SELECT b.title, p.page_no, sec.title AS section
            FROM pages p JOIN books b ON b.id=p.book_id
            LEFT JOIN sections sec ON sec.id=p.section_id
            WHERE b.id=:i
              AND p.search_tsv @@ phraseto_tsquery('simple', arabic_normalize('الامام الصادق'))
        """), {"i": int(TEST_ID)})).one()
    assert hit.page_no == 1
    assert hit.section == "الباب الأول"
