"""Simple internal control panel: browse categories and books, edit a book's category/
title/author, and add a new book -- either by uploading a real .abx source (which runs
through the exact same converter/validator/importer pipeline as the bulk import) or by
typing in bare catalog metadata with no content yet.

Server-rendered HTML behind HTTP Basic Auth. Deliberately outside the /api prefix and
the SearchHit/BookOut schemas the iOS app consumes -- this is an admin-only tool, not
part of the public contract, so it's free to change shape without touching the app.
"""

from __future__ import annotations

import importlib.util
import secrets
import sys
from html import escape
from pathlib import Path

from fastapi import APIRouter, Depends, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.exceptions import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.services.arabic import normalize

router = APIRouter(prefix="/admin", tags=["admin"])
_security = HTTPBasic()

_PAGE_SIZE = 50
# Real Shamela IDs top out in the 19,000s; manually-added books start well clear of that
# range so they can never collide with a future real import.
_MANUAL_ID_FLOOR = 900_000


def _require_admin(credentials: HTTPBasicCredentials = Depends(_security)) -> None:
    settings = get_settings()
    user_ok = secrets.compare_digest(credentials.username, settings.admin_username)
    pass_ok = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401, detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


# ── HTML shell ──────────────────────────────────────────────────────────────

_PAGE = """<!doctype html>
<html dir="rtl" lang="ar">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ font-family: Tahoma, -apple-system, sans-serif; margin: 2rem; background: #f7f7f5; color: #222; }}
  a {{ color: #1a5276; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  table {{ border-collapse: collapse; width: 100%; background: white; margin-bottom: 1rem; }}
  th, td {{ border: 1px solid #ddd; padding: 0.5rem; text-align: right; }}
  th {{ background: #eee; }}
  input, select, textarea {{ padding: 0.4rem; margin: 0.2rem 0; width: 100%; max-width: 30rem;
    box-sizing: border-box; font-family: inherit; }}
  form {{ background: white; padding: 1rem; margin-bottom: 1.5rem; border: 1px solid #ddd;
    max-width: 40rem; }}
  .row {{ margin-bottom: 0.8rem; }}
  label {{ display: block; font-weight: bold; margin-bottom: 0.2rem; }}
  button {{ padding: 0.5rem 1.2rem; background: #1a5276; color: white; border: none;
    cursor: pointer; }}
  .msg {{ padding: 0.6rem 1rem; margin-bottom: 1rem; border-radius: 3px; }}
  .msg.ok {{ background: #d4edda; }}
  .msg.err {{ background: #f8d7da; }}
  nav {{ margin-bottom: 1.5rem; }}
  .pager a {{ margin-left: 0.8rem; }}
  small {{ color: #666; }}
</style>
</head>
<body>
<nav><a href="/admin">لوحة التحكم</a> &nbsp;|&nbsp; <a href="/admin/books/new">إضافة كتاب</a></nav>
{body}
</body>
</html>"""


def _render(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=escape(title), body=body))


def _msg(text_: str, ok: bool) -> str:
    return f'<div class="msg {"ok" if ok else "err"}">{escape(text_)}</div>'


# ── Reused import pipeline (dynamic-loaded, same trick the test suite uses -- the
# converter/importer live as standalone scripts, not an importable package) ─────────

_import_books = None


def _importer():
    global _import_books
    if _import_books is None:
        path = Path(__file__).resolve().parents[2] / "scripts" / "import" / "import_books.py"
        spec = importlib.util.spec_from_file_location("import_books", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("import_books", module)
        spec.loader.exec_module(module)
        _import_books = module
    return _import_books


async def _get_or_create_author(session: AsyncSession, name: str) -> int | None:
    """Deliberately simpler than the bulk importer's version: no death-year handling,
    since a name typed by hand in this panel has no سنة الوفاة to attach. Matches an
    existing name-only author (NULL death_label) rather than creating a duplicate."""
    name = (name or "").strip()
    if not name:
        return None
    return await session.scalar(
        text("""
            INSERT INTO authors (name, name_norm)
            VALUES (:name, :norm)
            ON CONFLICT (name_norm, death_label) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
        """),
        {"name": name, "norm": normalize(name)},
    )


async def _next_manual_id(session: AsyncSession) -> int:
    return await session.scalar(
        text("SELECT GREATEST(COALESCE(MAX(id), 0), :floor) + 1 FROM books"),
        {"floor": _MANUAL_ID_FLOOR - 1},
    )


# ── Dashboard: subjects with counts ──────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
async def dashboard(
    session: AsyncSession = Depends(get_session), _: None = Depends(_require_admin)
) -> HTMLResponse:
    rows = (await session.execute(text("""
        SELECT s.id, s.title, count(DISTINCT w.id) AS work_count, count(b.id) AS book_count
        FROM subjects s
        LEFT JOIN works w ON w.subject_id = s.id
        LEFT JOIN books b ON b.work_id = w.id
        GROUP BY s.id, s.title, s.sort_order
        ORDER BY s.sort_order
    """))).all()
    rows_html = "".join(
        f'<tr><td><a href="/admin/subjects/{escape(r.id)}">{escape(r.title)}</a></td>'
        f"<td>{r.work_count}</td><td>{r.book_count}</td></tr>"
        for r in rows
    )
    body = (
        "<h1>التصنيفات</h1>"
        "<table><tr><th>التصنيف</th><th>عدد العناوين</th><th>عدد المجلدات</th></tr>"
        f"{rows_html}</table>"
    )
    return _render("لوحة التحكم", body)


# ── Subject detail: paginated works list ─────────────────────────────────────


@router.get("/subjects/{subject_id}", response_class=HTMLResponse)
async def subject_detail(
    subject_id: str,
    page: int = 1,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_admin),
) -> HTMLResponse:
    subject = (await session.execute(
        text("SELECT title FROM subjects WHERE id = :id"), {"id": subject_id}
    )).first()
    if subject is None:
        raise HTTPException(status_code=404, detail="Unknown subject")

    page = max(1, page)
    offset = (page - 1) * _PAGE_SIZE
    rows = (await session.execute(
        text("""
            SELECT w.id AS work_id, w.title, a.name AS author, w.volume_count,
                   count(b.id) AS book_count
            FROM works w
            LEFT JOIN authors a ON a.id = w.author_id
            LEFT JOIN books b ON b.work_id = w.id
            WHERE w.subject_id = :sid
            GROUP BY w.id, w.title, a.name, w.volume_count
            ORDER BY w.title
            LIMIT :limit OFFSET :offset
        """),
        {"sid": subject_id, "limit": _PAGE_SIZE, "offset": offset},
    )).all()

    rows_html = "".join(
        f"<tr><td>{escape(r.title)}</td><td>{escape(r.author or '')}</td>"
        f"<td>{r.book_count}</td>"
        f'<td><a href="/admin/works/{r.work_id}">عرض المجلدات</a></td></tr>'
        for r in rows
    )
    pager = (
        f'<div class="pager">'
        + (f'<a href="?page={page - 1}">السابق</a>' if page > 1 else "")
        + (f'<a href="?page={page + 1}">التالي</a>' if len(rows) == _PAGE_SIZE else "")
        + "</div>"
    )
    body = (
        f"<h1>{escape(subject.title)}</h1>"
        "<table><tr><th>العنوان</th><th>المؤلف</th><th>عدد المجلدات</th><th></th></tr>"
        f"{rows_html}</table>{pager}"
    )
    return _render(subject.title, body)


# ── Work detail: its volumes, each linking to the edit page ─────────────────


@router.get("/works/{work_id}", response_class=HTMLResponse)
async def work_detail(
    work_id: int,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_admin),
) -> HTMLResponse:
    work = (await session.execute(
        text("SELECT title FROM works WHERE id = :id"), {"id": work_id}
    )).first()
    if work is None:
        raise HTTPException(status_code=404, detail="Unknown work")

    rows = (await session.execute(
        text("""
            SELECT id, volume, title, is_published, page_count
            FROM books WHERE work_id = :wid ORDER BY volume NULLS FIRST, id
        """),
        {"wid": work_id},
    )).all()
    rows_html = "".join(
        f"<tr><td>{r.volume or '-'}</td><td>{escape(r.title)}</td>"
        f"<td>{'منشور' if r.is_published else 'غير منشور'}</td>"
        f"<td>{r.page_count or 0}</td>"
        f'<td><a href="/admin/books/{r.id}">تعديل</a></td></tr>'
        for r in rows
    )
    body = (
        f"<h1>{escape(work.title)}</h1>"
        "<table><tr><th>المجلد</th><th>العنوان</th><th>الحالة</th><th>الصفحات</th><th></th></tr>"
        f"{rows_html}</table>"
    )
    return _render(work.title, body)


# ── Book edit ────────────────────────────────────────────────────────────────


async def _subject_options(session: AsyncSession, selected: str | None) -> str:
    rows = (await session.execute(
        text("SELECT id, title FROM subjects ORDER BY sort_order")
    )).all()
    return "".join(
        f'<option value="{escape(r.id)}"{" selected" if r.id == selected else ""}>'
        f"{escape(r.title)}</option>"
        for r in rows
    )


@router.get("/books/{book_id}", response_class=HTMLResponse)
async def book_edit_form(
    book_id: int,
    saved: bool = False,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_admin),
) -> HTMLResponse:
    row = (await session.execute(
        text("""
            SELECT b.title, b.volume, b.is_published, a.name AS author,
                   w.subject_id, w.id AS work_id
            FROM books b
            JOIN works w ON w.id = b.work_id
            LEFT JOIN authors a ON a.id = b.author_id
            WHERE b.id = :id
        """),
        {"id": book_id},
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown book")

    options = await _subject_options(session, row.subject_id)
    banner = _msg("تم الحفظ", True) if saved else ""
    body = f"""
    {banner}
    <h1>تعديل الكتاب #{book_id}</h1>
    <form method="post" action="/admin/books/{book_id}">
      <div class="row"><label>العنوان</label>
        <input name="title" value="{escape(row.title)}" required></div>
      <div class="row"><label>المؤلف</label>
        <input name="author" value="{escape(row.author or '')}"></div>
      <div class="row"><label>التصنيف (يطبق على كل مجلدات هذا العنوان)</label>
        <select name="subject">{options}</select></div>
      <div class="row"><label>رقم المجلد</label>
        <input name="volume" type="number" value="{row.volume or ''}"></div>
      <div class="row"><label>
        <input name="is_published" type="checkbox" style="width:auto"
          {"checked" if row.is_published else ""}> منشور</label></div>
      <button type="submit">حفظ</button>
    </form>
    """
    return _render(f"تعديل #{book_id}", body)


@router.post("/books/{book_id}")
async def book_edit_save(
    book_id: int,
    title: str = Form(...),
    author: str = Form(""),
    subject: str = Form(...),
    volume: str = Form(""),
    is_published: bool = Form(False),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_admin),
) -> RedirectResponse:
    exists = await session.scalar(
        text("SELECT work_id FROM books WHERE id = :id"), {"id": book_id}
    )
    if exists is None:
        raise HTTPException(status_code=404, detail="Unknown book")

    author_id = await _get_or_create_author(session, author)
    volume_int = int(volume) if volume.strip().isdigit() else None

    await session.execute(
        text("""
            UPDATE books SET title = :title, title_norm = :norm, author_id = :author,
                   volume = :vol, is_published = :pub, updated_at = now()
            WHERE id = :id
        """),
        {"title": title, "norm": normalize(title), "author": author_id,
         "vol": volume_int, "pub": is_published, "id": book_id},
    )
    await session.execute(
        text("UPDATE works SET subject_id = :sid WHERE id = :wid"),
        {"sid": subject, "wid": exists},
    )
    await session.commit()
    return RedirectResponse(f"/admin/books/{book_id}?saved=1", status_code=303)


# ── Add a book ───────────────────────────────────────────────────────────────


@router.get("/books/new", response_class=HTMLResponse)
async def new_book_form(
    ok: str | None = None,
    err: str | None = None,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_admin),
) -> HTMLResponse:
    options = await _subject_options(session, None)
    banner = _msg(ok, True) if ok else (_msg(err, False) if err else "")
    body = f"""
    {banner}
    <h1>رفع ملف abx.</h1>
    <p><small>يمر عبر نفس خط المعالجة والتحقق والاستيراد المستخدم للمكتبة كاملة.</small></p>
    <form method="post" action="/admin/books/new/upload" enctype="multipart/form-data">
      <div class="row"><label>ملف .abx</label>
        <input name="file" type="file" accept=".abx" required></div>
      <div class="row"><label>رقم الكتاب (اختياري -- يُخصص تلقائياً إذا ترك فارغاً)</label>
        <input name="book_id" type="number"></div>
      <button type="submit">رفع واستيراد</button>
    </form>

    <h1>إضافة سجل يدوي (بدون محتوى)</h1>
    <p><small>يُنشئ سجلاً في الفهرس فقط -- بلا صفحات قابلة للبحث أو التنزيل حتى تتم إضافة محتوى لاحقاً.
    يبقى غير منشور تلقائياً.</small></p>
    <form method="post" action="/admin/books/new/manual">
      <div class="row"><label>العنوان</label><input name="title" required></div>
      <div class="row"><label>المؤلف</label><input name="author"></div>
      <div class="row"><label>التصنيف</label><select name="subject" required>{options}</select></div>
      <div class="row"><label>اللغة</label>
        <select name="language"><option value="ar">عربي</option><option value="fa">فارسي</option></select></div>
      <button type="submit">إنشاء</button>
    </form>
    """
    return _render("إضافة كتاب", body)


@router.post("/books/new/upload")
async def new_book_upload(
    file: UploadFile,
    book_id: str = Form(""),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_admin),
) -> RedirectResponse:
    settings = get_settings()
    resolved_id = int(book_id) if book_id.strip().isdigit() else await _next_manual_id(session)

    tmp_dir = settings.books_root.parent / "_admin_uploads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    source_path = tmp_dir / f"{resolved_id}.abx"
    source_path.write_bytes(await file.read())

    importer = _importer()
    try:
        result = await importer.import_one(
            session, source_path, "admin-panel", settings.books_root, True
        )
    finally:
        source_path.unlink(missing_ok=True)

    if result == "ok":
        return RedirectResponse(
            f"/admin/books/new?ok=تم استيراد الكتاب رقم {resolved_id} بنجاح", status_code=303
        )
    return RedirectResponse(
        f"/admin/books/new?err=فشل الاستيراد ({result}) -- راجع سجل الاستيراد لمعرفة السبب",
        status_code=303,
    )


@router.post("/books/new/manual")
async def new_book_manual(
    title: str = Form(...),
    author: str = Form(""),
    subject: str = Form(...),
    language: str = Form("ar"),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_admin),
) -> RedirectResponse:
    author_id = await _get_or_create_author(session, author)
    work_id = await session.scalar(
        text("""
            INSERT INTO works (title, title_norm, author_id, subject_id, language_code)
            VALUES (:title, :norm, :author, :subject, :lang)
            ON CONFLICT (title_norm, author_id) DO UPDATE SET
                title = EXCLUDED.title, subject_id = EXCLUDED.subject_id
            RETURNING id
        """),
        {"title": title, "norm": normalize(title), "author": author_id,
         "subject": subject, "lang": language},
    )
    book_id = await _next_manual_id(session)
    await session.execute(
        text("""
            INSERT INTO books (id, work_id, title, title_norm, author_id, language_code,
                                is_published)
            VALUES (:id, :work, :title, :norm, :author, :lang, false)
        """),
        {"id": book_id, "work": work_id, "title": title, "norm": normalize(title),
         "author": author_id, "lang": language},
    )
    await session.commit()
    return RedirectResponse(f"/admin/books/{book_id}", status_code=303)
