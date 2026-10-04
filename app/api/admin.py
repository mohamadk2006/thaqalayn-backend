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
import io
import json
import re
import secrets
import shutil
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import docx
from docx.oxml.ns import qn
from fastapi import APIRouter, Depends, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.exceptions import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.books import _resolve_under_root
from app.config import get_settings
from app.db import get_session
from app.services import drafts
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
  .checkbox-group {{ max-height: 14rem; overflow-y: auto; border: 1px solid #ddd;
    padding: 0.4rem 0.6rem; background: #fafafa; }}
  .checkbox-row {{ display: block; font-weight: normal; margin: 0.2rem 0; }}
  .checkbox-row input {{ width: auto; margin-left: 0.5rem; }}
</style>
</head>
<body>
<nav><a href="/admin">لوحة التحكم</a> &nbsp;|&nbsp; <a href="/admin/libraries">المكتبات</a>
&nbsp;|&nbsp; <a href="/admin/books/new">إضافة كتاب</a>
&nbsp;|&nbsp; <a href="/admin/drafts">مراجعة الكتب المحوّلة</a>
&nbsp;|&nbsp; <form method="get" action="/admin/search" style="display:inline">
<input name="q" placeholder="بحث بالعنوان أو المؤلف..." style="width:16rem; display:inline; margin:0">
</form></nav>
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
        LEFT JOIN work_subjects ws ON ws.subject_id = s.id
        LEFT JOIN works w ON w.id = ws.work_id
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
        '<p><a href="/admin/featured">الكتب المختارة &rarr;</a> &nbsp;|&nbsp; '
        '<a href="/admin/categories">ترتيب التصنيفات &rarr;</a></p>'
        "<h1>التصنيفات</h1>"
        "<table><tr><th>التصنيف</th><th>عدد العناوين</th><th>عدد المجلدات</th></tr>"
        f"{rows_html}</table>"
    )
    return _render("لوحة التحكم", body)


# ── Category order and sections ───────────────────────────────────────────────
#
# The app shows categories in two sections -- "shia" (الكتب الشيعية) then "other" (الكتب
# الأخرى) -- in sort_order. A section is a contiguous run of that list, so it is modelled as
# a boundary (how many come first) rather than a per-row flag that could be set into a
# pattern the app cannot draw. Moving a row up or down swaps it with its neighbour and the
# boundary stays where it is, so the row crossing it simply joins the other section.

_SECTIONS = (("shia", "الكتب الشيعية"), ("other", "الكتب الأخرى"))


async def _category_list(session: AsyncSession):
    return (await session.execute(text("""
        SELECT s.id, s.title, s.section,
               (SELECT count(DISTINCT ws.work_id) FROM work_subjects ws WHERE ws.subject_id = s.id) AS works,
               (SELECT string_agg(CAST(p.book_id AS text), '، ' ORDER BY p.position)
                FROM subject_pinned_books p WHERE p.subject_id = s.id) AS pinned
        FROM subjects s ORDER BY s.sort_order, s.id
    """))).all()


async def _save_category_order(session: AsyncSession, ids: list[str], shia_count: int) -> None:
    """Write 1..n and each row's section from its position. Only rows that actually change
    fire the change-tracking triggers, so the app's category-list version moves only if the
    list really did."""
    await session.execute(
        text("""
            UPDATE subjects s SET sort_order = v.ord, section = v.section
            FROM unnest(CAST(:ids AS text[]), CAST(:sections AS text[]))
                 WITH ORDINALITY AS v (id, section, ord)
            WHERE s.id = v.id
        """),
        {
            "ids": ids,
            "sections": ["shia" if i < shia_count else "other" for i in range(len(ids))],
        },
    )
    await session.commit()


@router.get("/categories", response_class=HTMLResponse)
async def category_order(
    ok: str | None = None,
    session: AsyncSession = Depends(get_session), _: None = Depends(_require_admin),
) -> HTMLResponse:
    rows = await _category_list(session)
    last = len(rows) - 1
    labels = dict(_SECTIONS)

    def button(subject_id: str, action: str, label: str, disabled: bool = False) -> str:
        if disabled:
            return f'<button type="button" disabled>{label}</button>'
        return (
            f'<form method="post" action="/admin/categories/{action}" style="display:inline; '
            f'background:none; border:none; padding:0; margin:0">'
            f'<input type="hidden" name="subject_id" value="{escape(subject_id)}">'
            + ('<input type="hidden" name="direction" value="{}">'.format("up" if "▲" in label else "down")
               if action == "move" else "")
            + f'<button type="submit">{label}</button></form>'
        )

    html, previous_section = [], None
    for i, r in enumerate(rows):
        if r.section != previous_section:
            html.append(f'<tr><th colspan="5">{labels[r.section]}</th></tr>')
            previous_section = r.section
        other = "الكتب الأخرى" if r.section == "shia" else "الكتب الشيعية"
        pinned = f"<small>مثبّت: {escape(r.pinned)}</small>" if r.pinned else ""
        html.append(
            "<tr>"
            f"<td>{i + 1}</td>"
            f"<td>{button(r.id, 'move', '▲', i == 0)} {button(r.id, 'move', '▼', i == last)}</td>"
            f'<td><a href="/admin/subjects/{escape(r.id)}">{escape(r.title)}</a> {pinned}</td>'
            f"<td>{r.works}</td>"
            f"<td>{button(r.id, 'section', 'نقل إلى ' + other)}</td>"
            "</tr>"
        )
    banner = _msg(ok, True) if ok else ""
    body = (
        f"{banner}<h1>ترتيب التصنيفات</h1>"
        "<p><small>هذا الترتيب هو ما يجلبه التطبيق من الخادم. زر ▲/▼ يبدّل التصنيف مع جاره؛ "
        "وإذا عبر الحدّ بين القسمين انتقل إلى القسم الآخر. «نقل إلى…» ينقله إلى بداية/نهاية القسم الآخر.</small></p>"
        "<table><tr><th>#</th><th>الترتيب</th><th>التصنيف</th><th>العناوين</th><th>القسم</th></tr>"
        f"{''.join(html)}</table>"
    )
    return _render("ترتيب التصنيفات", body)


@router.post("/categories/move")
async def category_move(
    subject_id: str = Form(...),
    direction: str = Form(...),
    session: AsyncSession = Depends(get_session), _: None = Depends(_require_admin),
) -> RedirectResponse:
    rows = await _category_list(session)
    ids = [r.id for r in rows]
    if subject_id not in ids:
        raise HTTPException(status_code=404, detail="Unknown category")
    shia = sum(1 for r in rows if r.section == "shia")
    i = ids.index(subject_id)
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
        await _save_category_order(session, ids, shia)
    return RedirectResponse("/admin/categories", status_code=303)


@router.post("/categories/section")
async def category_section(
    subject_id: str = Form(...),
    session: AsyncSession = Depends(get_session), _: None = Depends(_require_admin),
) -> RedirectResponse:
    """Move a category to the other section: the start of "other" if it was in "shia", the
    end of "shia" if it was in "other" -- the only places that keep both runs contiguous."""
    rows = await _category_list(session)
    ids = [r.id for r in rows]
    if subject_id not in ids:
        raise HTTPException(status_code=404, detail="Unknown category")
    shia = sum(1 for r in rows if r.section == "shia")
    was_shia = rows[ids.index(subject_id)].section == "shia"
    ids.remove(subject_id)
    if was_shia:
        shia -= 1
        ids.insert(shia, subject_id)      # first of "other"
    else:
        ids.insert(shia, subject_id)      # last of "shia"
        shia += 1
    await _save_category_order(session, ids, shia)
    return RedirectResponse("/admin/categories", status_code=303)


@router.get("/featured", response_class=HTMLResponse)
async def featured_list(
    session: AsyncSession = Depends(get_session), _: None = Depends(_require_admin)
) -> HTMLResponse:
    rows = (await session.execute(text("""
        SELECT w.id AS work_id, w.title, a.name AS author,
               string_agg(s.title, '، ' ORDER BY s.sort_order) AS subject_titles
        FROM works w
        LEFT JOIN authors a ON a.id = w.author_id
        LEFT JOIN work_subjects ws ON ws.work_id = w.id
        LEFT JOIN subjects s ON s.id = ws.subject_id
        WHERE w.is_featured
        GROUP BY w.id, w.title, a.name, w.featured_sort_order
        ORDER BY w.featured_sort_order, w.title_norm
    """))).all()
    last = len(rows) - 1

    def _move_form(work_id: int, direction: str, disabled: bool) -> str:
        if disabled:
            return f'<button type="button" disabled>{"▲" if direction == "up" else "▼"}</button>'
        return (
            f'<form method="post" action="/admin/featured/{work_id}/move" style="display:inline; '
            f'background:none; border:none; padding:0; margin:0">'
            f'<input type="hidden" name="direction" value="{direction}">'
            f'<button type="submit">{"▲" if direction == "up" else "▼"}</button></form>'
        )

    rows_html = "".join(
        "<tr>"
        f'<td>{_move_form(r.work_id, "up", i == 0)} {_move_form(r.work_id, "down", i == last)}</td>'
        f'<td><a href="/admin/works/{r.work_id}">{escape(r.title)}</a></td>'
        f"<td>{escape(r.author or '')}</td><td>{escape(r.subject_titles or '')}</td></tr>"
        for i, r in enumerate(rows)
    )
    body = (
        f"<h1>الكتب المختارة ({len(rows)})</h1>"
        "<p><small>يحدد هذا الترتيب تسلسل ظهور الكتب في واجهة \"المختارات\" داخل التطبيق.</small></p>"
        "<table><tr><th>الترتيب</th><th>العنوان</th><th>المؤلف</th><th>التصنيف</th></tr>"
        f"{rows_html}</table>"
    )
    return _render("الكتب المختارة", body)


@router.post("/featured/{work_id:int}/move")
async def featured_move(
    work_id: int,
    direction: str = Form(...),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_admin),
) -> RedirectResponse:
    """Swap this work's featured_sort_order with its immediate neighbor in the current
    featured ordering -- simplest reorder primitive that needs no drag-and-drop JS, and
    is naturally idempotent-safe against a double click (a neighbor missing at either
    end is just a no-op)."""
    rows = (await session.execute(text("""
        SELECT id, featured_sort_order FROM works
        WHERE is_featured ORDER BY featured_sort_order, title_norm
    """))).all()
    ids = [r.id for r in rows]
    if work_id not in ids:
        raise HTTPException(status_code=404, detail="Not in featured set")

    idx = ids.index(work_id)
    neighbor_idx = idx - 1 if direction == "up" else idx + 1
    if 0 <= neighbor_idx < len(rows):
        a, b = rows[idx], rows[neighbor_idx]
        await session.execute(
            text("UPDATE works SET featured_sort_order = :order WHERE id = :id"),
            [
                {"id": a.id, "order": b.featured_sort_order},
                {"id": b.id, "order": a.featured_sort_order},
            ],
        )
        await session.commit()
    return RedirectResponse("/admin/featured", status_code=303)


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
            JOIN work_subjects ws ON ws.work_id = w.id AND ws.subject_id = :sid
            LEFT JOIN authors a ON a.id = w.author_id
            LEFT JOIN books b ON b.work_id = w.id
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


# ── Libraries: open-ended, admin-creatable, independent of subjects ─────────


@router.get("/libraries", response_class=HTMLResponse)
async def library_list(
    session: AsyncSession = Depends(get_session), _: None = Depends(_require_admin)
) -> HTMLResponse:
    rows = (await session.execute(text("""
        SELECT l.id, l.title, l.parent_id, p.title AS parent_title,
               count(DISTINCT lw.work_id) AS work_count
        FROM libraries l
        LEFT JOIN libraries p ON p.id = l.parent_id
        LEFT JOIN library_works lw ON lw.library_id = l.id
        GROUP BY l.id, l.title, l.parent_id, p.title
        ORDER BY l.parent_id NULLS FIRST, l.sort_order
    """))).all()
    parent_options = "".join(
        f'<option value="{r.id}">{escape(r.title)}</option>' for r in rows
    )
    rows_html = "".join(
        f'<tr><td>{"&nbsp;&nbsp;&larr; " if r.parent_id else ""}'
        f'<a href="/admin/libraries/{r.id}">{escape(r.title)}</a></td>'
        f"<td>{escape(r.parent_title or '')}</td><td>{r.work_count}</td></tr>"
        for r in rows
    )
    body = f"""
    <h1>المكتبات</h1>
    <table><tr><th>الاسم</th><th>المكتبة الأم</th><th>عدد العناوين</th></tr>
      {rows_html or '<tr><td colspan="3">لا توجد مكتبات بعد</td></tr>'}
    </table>
    <h2>إضافة مكتبة جديدة</h2>
    <form method="post" action="/admin/libraries/new">
      <div class="row"><label>الاسم</label>
        <input name="title" required></div>
      <div class="row"><label>مكتبة أم (اختياري)</label>
        <select name="parent_id"><option value="">-- بلا --</option>{parent_options}</select></div>
      <button type="submit">إضافة</button>
    </form>
    """
    return _render("المكتبات", body)


@router.post("/libraries/new")
async def library_create(
    title: str = Form(...),
    parent_id: str = Form(""),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_admin),
) -> RedirectResponse:
    parent = int(parent_id) if parent_id.strip().isdigit() else None
    await session.execute(
        text("INSERT INTO libraries (title, parent_id) VALUES (:title, :parent)"),
        {"title": title, "parent": parent},
    )
    await session.commit()
    return RedirectResponse("/admin/libraries", status_code=303)


@router.get("/libraries/{library_id:int}", response_class=HTMLResponse)
async def library_detail(
    library_id: int,
    page: int = 1,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_admin),
) -> HTMLResponse:
    library = (await session.execute(
        text("SELECT title FROM libraries WHERE id = :id"), {"id": library_id}
    )).first()
    if library is None:
        raise HTTPException(status_code=404, detail="Unknown library")

    page = max(1, page)
    offset = (page - 1) * _PAGE_SIZE
    rows = (await session.execute(
        text("""
            SELECT w.id AS work_id, w.title, a.name AS author, count(b.id) AS book_count
            FROM works w
            JOIN library_works lw ON lw.work_id = w.id AND lw.library_id = :lid
            LEFT JOIN authors a ON a.id = w.author_id
            LEFT JOIN books b ON b.work_id = w.id
            GROUP BY w.id, w.title, a.name
            ORDER BY w.title
            LIMIT :limit OFFSET :offset
        """),
        {"lid": library_id, "limit": _PAGE_SIZE, "offset": offset},
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
        f"<h1>{escape(library.title)}</h1>"
        "<table><tr><th>العنوان</th><th>المؤلف</th><th>عدد المجلدات</th><th></th></tr>"
        f"{rows_html or '<tr><td colspan=4>لا توجد عناوين في هذه المكتبة بعد</td></tr>'}</table>{pager}"
    )
    return _render(library.title, body)


# ── Work detail: its volumes, each linking to the edit page ─────────────────


@router.get("/works/{work_id}", response_class=HTMLResponse)
async def work_detail(
    work_id: int,
    ok: str | None = None,
    err: str | None = None,
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
        f'<td><a href="/admin/books/{r.id}">تعديل</a> | '
        f'<a href="/admin/books/{r.id}/content">عرض المحتوى</a></td></tr>'
        for r in rows
    )
    next_volume = max((r.volume or 0 for r in rows), default=0) + 1
    banner = _msg(ok, True) if ok else (_msg(err, False) if err else "")
    body = (
        f"{banner}<h1>{escape(work.title)}</h1>"
        f"<p>رقم العمل: <strong>{work_id}</strong></p>"
        "<table><tr><th>المجلد</th><th>العنوان</th><th>الحالة</th><th>الصفحات</th><th></th></tr>"
        f"{rows_html}</table>"
        "<h2>إضافة مجلد جديد من ملف JSON</h2>"
        "<p><small>يأخذ المجلد عنوان هذا العمل ومؤلفه تلقائياً (فيُجمع معه دائماً)، ويُعطى رقماً جديداً.</small></p>"
        '<form method="post" action="/admin/books/new/json" enctype="multipart/form-data">'
        f'<input type="hidden" name="work_id" value="{work_id}">'
        '<div class="row"><label>ملف .json</label>'
        '<input name="file" type="file" accept=".json,application/json" required></div>'
        f'<div class="row"><label>رقم المجلد</label>'
        f'<input name="volume" type="number" min="1" value="{next_volume}" required></div>'
        '<div class="row"><label>رقم الكتاب (اختياري -- يُخصص تلقائياً إذا ترك فارغاً)</label>'
        '<input name="book_id" type="number"></div>'
        '<button type="submit">رفع واستيراد</button></form>'
    )
    return _render(work.title, body)


# ── Search by title/author ────────────────────────────────────────────────────


@router.get("/search", response_class=HTMLResponse)
async def search_works(
    q: str = "",
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_admin),
) -> HTMLResponse:
    q = q.strip()
    if not q:
        return _render("بحث", "<h1>بحث</h1><p>اكتب عنوان كتاب أو اسم مؤلف للبحث.</p>")

    # Escaped so a literal % or _ typed by the admin can't act as a LIKE wildcard.
    pattern = "%" + normalize(q).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = (await session.execute(
        text("""
            SELECT w.id AS work_id, w.title, a.name AS author,
                   count(b.id) AS book_count
            FROM works w
            LEFT JOIN authors a ON a.id = w.author_id
            LEFT JOIN books b ON b.work_id = w.id
            WHERE w.title_norm ILIKE :pattern ESCAPE '\\'
               OR a.name_norm ILIKE :pattern ESCAPE '\\'
            GROUP BY w.id, w.title, a.name
            ORDER BY w.title
            LIMIT 200
        """),
        {"pattern": pattern},
    )).all()

    rows_html = "".join(
        f'<tr><td><a href="/admin/works/{r.work_id}">{escape(r.title)}</a></td>'
        f"<td>{escape(r.author or '')}</td><td>{r.book_count}</td></tr>"
        for r in rows
    )
    body = (
        f"<h1>نتائج البحث عن &laquo;{escape(q)}&raquo; ({len(rows)})</h1>"
        + ("<table><tr><th>العنوان</th><th>المؤلف</th><th>عدد المجلدات</th></tr>"
           f"{rows_html}</table>" if rows else "<p>لا توجد نتائج.</p>")
    )
    return _render(f"بحث: {q}", body)


# ── Book edit ────────────────────────────────────────────────────────────────


async def _subject_checkboxes(session: AsyncSession, selected: set[str]) -> str:
    """A work can belong to more than one of the 39 subjects, so this is a checkbox
    group (one <input name="subject"> per checked box, collected server-side as a list)
    rather than the single-select dropdown it used to be."""
    rows = (await session.execute(
        text("SELECT id, title FROM subjects ORDER BY sort_order")
    )).all()
    return "".join(
        f'<label class="checkbox-row"><input type="checkbox" name="subject" '
        f'value="{escape(r.id)}"{" checked" if r.id in selected else ""}> '
        f"{escape(r.title)}</label>"
        for r in rows
    )


async def _work_subject_ids(session: AsyncSession, work_id: int) -> set[str]:
    rows = (await session.execute(
        text("SELECT subject_id FROM work_subjects WHERE work_id = :wid"), {"wid": work_id}
    )).scalars().all()
    return set(rows)


async def _set_work_subjects(session: AsyncSession, work_id: int, subject_ids: list[str]) -> None:
    """Replace a work's whole subject set with `subject_ids` -- delete then re-insert
    rather than diffing, since a work rarely has more than a couple of subjects and this
    is only ever called from a form submit carrying the complete intended set."""
    await session.execute(
        text("DELETE FROM work_subjects WHERE work_id = :wid"), {"wid": work_id}
    )
    if subject_ids:
        await session.execute(
            text("INSERT INTO work_subjects (work_id, subject_id) VALUES (:wid, :sid)"),
            [{"wid": work_id, "sid": sid} for sid in dict.fromkeys(subject_ids)],
        )


async def _library_checkboxes(session: AsyncSession, selected: set[int]) -> str:
    """Same shape as _subject_checkboxes, but for the independent, open-ended library
    system -- a work's libraries have nothing to do with its subjects."""
    rows = (await session.execute(
        text("SELECT id, title FROM libraries ORDER BY parent_id NULLS FIRST, sort_order")
    )).all()
    if not rows:
        return "<p><small>لا توجد مكتبات بعد -- <a href=\"/admin/libraries\">أضف واحدة</a></small></p>"
    return "".join(
        f'<label class="checkbox-row"><input type="checkbox" name="library" '
        f'value="{r.id}"{" checked" if r.id in selected else ""}> '
        f"{escape(r.title)}</label>"
        for r in rows
    )


async def _work_library_ids(session: AsyncSession, work_id: int) -> set[int]:
    rows = (await session.execute(
        text("SELECT library_id FROM library_works WHERE work_id = :wid"), {"wid": work_id}
    )).scalars().all()
    return set(rows)


async def _set_work_libraries(session: AsyncSession, work_id: int, library_ids: list[int]) -> None:
    """Replace a work's whole library set with `library_ids` -- same delete-then-
    re-insert approach as _set_work_subjects, for the same reason."""
    await session.execute(
        text("DELETE FROM library_works WHERE work_id = :wid"), {"wid": work_id}
    )
    if library_ids:
        await session.execute(
            text("INSERT INTO library_works (work_id, library_id) VALUES (:wid, :lid)"),
            [{"wid": work_id, "lid": lid} for lid in dict.fromkeys(library_ids)],
        )


@router.get("/books/{book_id:int}", response_class=HTMLResponse)
async def book_edit_form(
    book_id: int,
    saved: bool = False,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_admin),
) -> HTMLResponse:
    row = (await session.execute(
        text("""
            SELECT b.title, b.volume, b.is_published, a.name AS author,
                   w.id AS work_id, w.is_featured
            FROM books b
            JOIN works w ON w.id = b.work_id
            LEFT JOIN authors a ON a.id = b.author_id
            WHERE b.id = :id
        """),
        {"id": book_id},
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown book")

    selected = await _work_subject_ids(session, row.work_id)
    options = await _subject_checkboxes(session, selected)
    selected_libraries = await _work_library_ids(session, row.work_id)
    library_options = await _library_checkboxes(session, selected_libraries)
    banner = _msg("تم الحفظ", True) if saved else ""
    body = f"""
    {banner}
    <h1>تعديل الكتاب #{book_id}</h1>
    <p><a href="/admin/books/{book_id}/content">عرض المحتوى &rarr;</a></p>
    <form method="post" action="/admin/books/{book_id}">
      <div class="row"><label>العنوان</label>
        <input name="title" value="{escape(row.title)}" required></div>
      <div class="row"><label>المؤلف</label>
        <input name="author" value="{escape(row.author or '')}"></div>
      <div class="row"><label>التصنيف (يطبق على كل مجلدات هذا العنوان -- يمكن اختيار أكثر من واحد)</label>
        <div class="checkbox-group">{options}</div></div>
      <div class="row"><label>المكتبات (مستقلة عن التصنيف -- <a href="/admin/libraries">إدارة المكتبات</a>)</label>
        <div class="checkbox-group">{library_options}</div></div>
      <div class="row"><label>رقم المجلد</label>
        <input name="volume" type="number" value="{row.volume or ''}"></div>
      <div class="row"><label>
        <input name="is_published" type="checkbox" style="width:auto"
          {"checked" if row.is_published else ""}> منشور</label></div>
      <div class="row"><label>
        <input name="is_featured" type="checkbox" style="width:auto"
          {"checked" if row.is_featured else ""}> ضمن الكتب المختارة
        (يطبق على كل مجلدات هذا العنوان)</label></div>
      <button type="submit">حفظ</button>
    </form>
    """
    return _render(f"تعديل #{book_id}", body)


@router.post("/books/{book_id:int}")
async def book_edit_save(
    book_id: int,
    title: str = Form(...),
    author: str = Form(""),
    subject: list[str] = Form([]),
    library: list[int] = Form([]),
    volume: str = Form(""),
    is_published: bool = Form(False),
    is_featured: bool = Form(False),
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
    # Newly featured (was false, now true) goes to the end of the featured order,
    # rather than defaulting to 0 and colliding with everything else already there.
    await session.execute(
        text("""
            UPDATE works SET is_featured = :feat,
                featured_sort_order = CASE
                    WHEN :feat AND NOT is_featured
                    THEN (SELECT COALESCE(MAX(featured_sort_order), 0) + 1 FROM works WHERE is_featured)
                    ELSE featured_sort_order
                END
            WHERE id = :wid
        """),
        {"feat": is_featured, "wid": exists},
    )
    await _set_work_subjects(session, exists, subject)
    await _set_work_libraries(session, exists, library)
    await session.commit()
    return RedirectResponse(f"/admin/books/{book_id}?saved=1", status_code=303)


# ── Book content viewer ──────────────────────────────────────────────────────
#
# Reads the same JSON file the download endpoint serves and renders it the way v2
# actually organizes it: pages[] is the real unit -- a book is browsed one physical
# page at a time, exactly like the app itself and like Page/search_tsv on the server
# side. toc[] is flat, optional context (some sources have no فهرس الموضوعات at all)
# used only as a jump-list to a starting page, never as the thing being paginated
# through -- there is no chapter/section container to open any more.


def _page_lookup(content: dict) -> dict[int, dict]:
    return {p["sequence"]: p for p in content.get("pages", [])}


@router.get("/books/{book_id:int}/content", response_class=HTMLResponse)
async def book_content(
    book_id: int,
    page: int | None = None,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_admin),
) -> HTMLResponse:
    row = (await session.execute(
        text("SELECT title, content_path FROM books WHERE id = :id"), {"id": book_id}
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown book")
    if not row.content_path:
        body = (
            f"<h1>{escape(row.title)}</h1>"
            "<p>لا يوجد محتوى بعد لهذا الكتاب.</p>"
            f'<p><a href="/admin/books/{book_id}">&larr; رجوع</a></p>'
        )
        return _render(row.title, body)

    settings = get_settings()
    path = _resolve_under_root(settings.books_root, row.content_path)
    if not path.is_file():
        body = (
            f"<h1>{escape(row.title)}</h1>"
            "<p>سجل المحتوى موجود في قاعدة البيانات لكن الملف غير موجود على القرص.</p>"
            f'<p><a href="/admin/books/{book_id}">&larr; رجوع</a></p>'
        )
        return _render(row.title, body)

    content = json.loads(path.read_text(encoding="utf-8"))
    pages = sorted(content.get("pages", []), key=lambda p: p.get("sequence", 0))
    if not pages:
        body = (
            f"<h1>{escape(row.title)}</h1>"
            "<p>لا توجد صفحات في هذا الملف.</p>"
            f'<p><a href="/admin/books/{book_id}">&larr; رجوع</a></p>'
        )
        return _render(row.title, body)

    if page is None:
        toc = sorted(content.get("toc", []), key=lambda e: e.get("order", 0))
        page_by_id = {p["id"]: p for p in pages}
        toc_html = "".join(
            f'<tr><td>{escape(entry.get("title") or "")}</td>'
            f'<td>{escape(str(page_by_id[entry["pageId"]]["pageNumber"])) if entry.get("pageId") in page_by_id else ""}</td>'
            f'<td><a href="?page={page_by_id[entry["pageId"]]["sequence"]}">فتح</a></td></tr>'
            for entry in toc if entry.get("pageId") in page_by_id
        ) or "<tr><td colspan=3>لا يوجد فهرس موضوعات لهذا الكتاب</td></tr>"
        body = (
            f"<h1>{escape(row.title)}</h1>"
            f"<p><small>{escape(content.get('author') or '')}</small></p>"
            f'<p><a href="?page={pages[0]["sequence"]}">&rarr; ابدأ من الصفحة الأولى</a>'
            f' ({len(pages)} صفحة)</p>'
            "<h2>فهرس الموضوعات</h2>"
            "<table><tr><th>العنوان</th><th>الصفحة</th><th></th></tr>"
            f"{toc_html}</table>"
            f'<p><a href="/admin/books/{book_id}">&larr; رجوع للتعديل</a></p>'
        )
        return _render(row.title, body)

    lookup = _page_lookup(content)
    current = lookup.get(page)
    if current is None:
        raise HTTPException(status_code=404, detail="Unknown page")

    blocks_html = []
    for block in sorted(current.get("blocks", []), key=lambda b: b.get("order", 0)):
        text_ = escape(block.get("text") or "")
        if block.get("type") == "heading":
            blocks_html.append(f"<p><strong>{text_}</strong></p>")
        elif block.get("type") == "footnotes":
            blocks_html.append(f"<p><small>{text_}</small></p>")
        else:
            blocks_html.append(f"<p>{text_}</p>")
    if current.get("isBlank"):
        blocks_html.append("<p><small>(صفحة بيضاء)</small></p>")

    min_seq, max_seq = pages[0]["sequence"], pages[-1]["sequence"]
    nav = (
        (f'<a href="?page={page - 1}">&larr; السابقة</a>' if page > min_seq else "")
        + " &nbsp;|&nbsp; "
        + f'<a href="/admin/books/{book_id}/content">الفهرس</a>'
        + " &nbsp;|&nbsp; "
        + (f'<a href="?page={page + 1}">التالية &rarr;</a>' if page < max_seq else "")
    )
    body = (
        f"<h1>{escape(row.title)} -- صفحة {escape(str(current['pageNumber']))}</h1>"
        f"<p>{nav}</p>"
        f"{''.join(blocks_html)}"
        f"<p>{nav}</p>"
    )
    return _render(row.title, body)


# ── Word document import ──────────────────────────────────────────────────────
#
# A .docx has no stored concept of rendered page numbers -- Word computes those at
# layout time and never writes them to the file. The one page signal a .docx *can*
# carry is an explicit manual break (Ctrl+Enter, <w:br w:type="page"/>), which is
# exactly what a source with real, meaningful page boundaries uses. Anything without
# one of those stays on whatever page came before it -- there is no other basis to
# invent a boundary from.
#
# Converting to a synthetic Shamela-style .abx text stream (rather than building a new
# import path) means this reuses the exact same convert()/validate()/import_one()
# pipeline every other book in the library goes through -- the result is
# structurally identical by construction, not just by convention.


_W_T = qn("w:t")
_W_TAB = qn("w:tab")
_W_BR = qn("w:br")


def _paragraph_page_segments(para) -> list[str]:
    """Split one paragraph's own text at each manual page break, in document order.

    A break very often lands mid-paragraph -- text typed, then Ctrl+Enter, more text
    typed into what is still (to Word) the same paragraph. Treating the break as a
    paragraph-level flag would put everything typed *before* it on the wrong page;
    walking each run's child nodes in order is what actually locates the break between
    two runs of real text instead of before or after the whole paragraph.
    """
    segments = [""]
    for run in para.runs:
        for child in run._element:
            if child.tag == _W_T:
                segments[-1] += child.text or ""
            elif child.tag == _W_TAB:
                segments[-1] += "\t"
            elif child.tag == _W_BR:
                if child.get(qn("w:type")) == "page":
                    segments.append("")
                else:
                    segments[-1] += " "  # a soft line break, not a page boundary
    return segments


def _docx_body_lines(data: bytes) -> list[str]:
    document = docx.Document(io.BytesIO(data))
    lines: list[str] = ["< صفحة > 1 < / صفحة >"]
    page = 1
    for para in document.paragraphs:
        style = para.style.name if para.style else ""
        is_heading = style.startswith("Heading") or style == "Title"

        for i, segment in enumerate(_paragraph_page_segments(para)):
            if i > 0:
                page += 1
                lines.append(f"< صفحة > {page} < / صفحة >")
            text_ = segment.strip()
            if not text_:
                continue
            if is_heading:
                lines.append("< فهرس الموضوعات >")
                lines.append(text_)
                lines.append("< / فهرس الموضوعات >")
            else:
                lines.append(text_)
    return lines


def _abx_source_text(title: str, author: str, death: str, body_lines: list[str]) -> str:
    # Angle brackets in a title/author would be indistinguishable from the tag grammar
    # itself -- strip them rather than trying to escape something the format has no
    # escaping mechanism for.
    def clean(s: str) -> str:
        return s.replace("<", "").replace(">", "").strip()

    header = [
        "checksum-not-applicable",
        f"< اسم الكتاب > {clean(title)} < / اسم الكتاب >",
        f"< اسم المؤلف > {clean(author)} < / اسم المؤلف >",
    ]
    if death.strip():
        header.append(f"< سنة الوفاة > {clean(death)} < / سنة الوفاة >")
    header.append("< الكتاب >")
    return "\n".join(header + body_lines + ["< / الكتاب >"])


@router.post("/books/new/docx")
async def new_book_docx(
    file: UploadFile,
    title: str = Form(...),
    author: str = Form(""),
    death: str = Form(""),
    subject: list[str] = Form(...),
    language: str = Form("ar"),
    book_id: str = Form(""),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_admin),
) -> RedirectResponse:
    settings = get_settings()
    resolved_id = int(book_id) if book_id.strip().isdigit() else await _next_manual_id(session)

    try:
        body_lines = _docx_body_lines(await file.read())
    except Exception as exc:  # python-docx raises plain Exception/PackageNotFoundError
        return RedirectResponse(
            f"/admin/books/new?err=تعذّرت قراءة ملف Word (تأكد أنه .docx وليس .doc القديم): {exc}",
            status_code=303,
        )

    tmp_dir = settings.books_root.parent / "_admin_uploads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    source_path = tmp_dir / f"{resolved_id}.abx"
    source_path.write_text(
        _abx_source_text(title, author, death, body_lines), encoding="utf-8"
    )

    importer = _importer()
    try:
        result = await importer.import_one(
            session, source_path, "admin-panel-docx", settings.books_root, True
        )
    finally:
        source_path.unlink(missing_ok=True)

    if result != "ok":
        return RedirectResponse(
            f"/admin/books/new?err=فشل الاستيراد ({result})", status_code=303
        )

    # subject/language come from the form, not a مجموعة string to classify -- import_one
    # leaves the work unclassified and its language at the "ar" default for a source
    # with no Shamela collection, so both are set directly here instead.
    work_id = await session.scalar(
        text("SELECT work_id FROM books WHERE id = :id"), {"id": resolved_id}
    )
    await session.execute(
        text("UPDATE works SET language_code = :lang WHERE id = :wid"),
        {"lang": language, "wid": work_id},
    )
    await _set_work_subjects(session, work_id, subject)
    await session.execute(
        text("UPDATE books SET language_code = :lang WHERE id = :id"),
        {"lang": language, "id": resolved_id},
    )
    await session.commit()
    return RedirectResponse(
        f"/admin/books/new?ok=تم استيراد الكتاب رقم {resolved_id} من ملف Word بنجاح",
        status_code=303,
    )


# ── Add a book ───────────────────────────────────────────────────────────────


@router.get("/books/new", response_class=HTMLResponse)
async def new_book_form(
    ok: str | None = None,
    err: str | None = None,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_admin),
) -> HTMLResponse:
    options = await _subject_checkboxes(session, set())
    library_options = await _library_checkboxes(session, set())
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

    <h1>رفع ملف JSON (v2)</h1>
    <p><small>ملف كتاب جاهز بصيغة v2 (ناتج المحوّل أو محرر JSON). يمر عبر نفس التحقق والاستيراد.
    <b>الكتاب متعدد المجلدات:</b> كل مجلد ملف JSON مستقل، بالعنوان والمؤلف نفسيهما تماماً ورقم مجلد مختلف --
    ارفع المجلد الأول من هنا، ثم أضف بقية المجلدات من زر «إضافة مجلد جديد» في صفحة العمل
    (يضمن تطابق العنوان والمؤلف). رقم الكتاب الموجود في الملف يُتجاهل ويُخصص رقم جديد.</small></p>
    <form method="post" action="/admin/books/new/json" enctype="multipart/form-data">
      <div class="row"><label>ملف .json</label>
        <input name="file" type="file" accept=".json,application/json" required></div>
      <div class="row"><label>رقم المجلد (اختياري -- للكتب متعددة المجلدات)</label>
        <input name="volume" type="number" min="1"></div>
      <div class="row"><label>رقم الكتاب (اختياري -- يُخصص تلقائياً إذا ترك فارغاً)</label>
        <input name="book_id" type="number"></div>
      <div class="row"><label>التصنيف (اختياري)</label>
        <div class="checkbox-group">{options}</div></div>
      <div class="row"><label>المكتبات (اختياري)</label>
        <div class="checkbox-group">{library_options}</div></div>
      <button type="submit">رفع واستيراد</button>
    </form>

    <h1>رفع ملف Word (.docx)</h1>
    <p><small>يحوَّل إلى نفس البنية المستخدمة في المكتبة: عناوين Word (Heading) تصبح عناوين أقسام،
    وفواصل الصفحات اليدوية في Word (Ctrl+Enter) تصبح أرقام صفحات. بلا فاصل صفحة يدوي، يبقى النص
    على نفس رقم الصفحة السابق -- لا يوجد أساس آخر لتخمين حدود الصفحة.</small></p>
    <form method="post" action="/admin/books/new/docx" enctype="multipart/form-data">
      <div class="row"><label>ملف .docx</label>
        <input name="file" type="file" accept=".docx" required></div>
      <div class="row"><label>العنوان</label><input name="title" required></div>
      <div class="row"><label>المؤلف</label><input name="author"></div>
      <div class="row"><label>سنة الوفاة (اختياري)</label><input name="death"></div>
      <div class="row"><label>التصنيف (يمكن اختيار أكثر من واحد)</label>
        <div class="checkbox-group">{options}</div></div>
      <div class="row"><label>اللغة</label>
        <select name="language"><option value="ar">عربي</option><option value="fa">فارسي</option></select></div>
      <div class="row"><label>رقم الكتاب (اختياري)</label>
        <input name="book_id" type="number"></div>
      <button type="submit">رفع واستيراد</button>
    </form>

    <h1>إضافة سجل يدوي (بدون محتوى)</h1>
    <p><small>يُنشئ سجلاً في الفهرس فقط -- بلا صفحات قابلة للبحث أو التنزيل حتى تتم إضافة محتوى لاحقاً.
    يبقى غير منشور تلقائياً.</small></p>
    <form method="post" action="/admin/books/new/manual">
      <div class="row"><label>العنوان</label><input name="title" required></div>
      <div class="row"><label>المؤلف</label><input name="author"></div>
      <div class="row"><label>التصنيف (يمكن اختيار أكثر من واحد)</label>
        <div class="checkbox-group">{options}</div></div>
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


@router.post("/books/new/json")
async def new_book_json(
    file: UploadFile,
    book_id: str = Form(""),
    volume: str = Form(""),
    work_id: str = Form(""),
    subject: list[str] = Form([]),
    library: list[int] = Form([]),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_admin),
) -> RedirectResponse:
    """Import an already-converted v2 book JSON through the same validate + upsert path
    as every other book. A multi-volume book is one file per volume, grouped into a work
    by identical (title, author): when `work_id` is given the volume is forced onto that
    work's own title/author/death label so it can never split off into a new work."""
    back = f"/admin/works/{work_id}" if work_id.strip().isdigit() else "/admin/books/new"
    try:
        content = json.loads((await file.read()).decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return RedirectResponse(f"{back}?err=الملف ليس JSON صالحاً: {exc}", status_code=303)
    new_book, new_work, error = await _import_book_json(
        session, content, book_id=book_id, volume=volume, work_id=work_id,
        subject=subject, library=library, source="admin-panel-json",
    )
    if error:
        return RedirectResponse(f"{back}?err={error}", status_code=303)
    return RedirectResponse(
        f"/admin/works/{new_work}?ok=تم استيراد الكتاب رقم {new_book}", status_code=303
    )


async def _import_book_json(
    session: AsyncSession, content, *, book_id: str = "", volume: str = "", work_id: str = "",
    subject: list[str] | None = None, library: list[int] | None = None, source: str,
    replace_book_id: str = "", confirm_mismatch: bool = False, report: dict | None = None,
) -> tuple[int | None, int | None, str | None]:
    """Validate and import a v2 book JSON -- the JSON upload form and the conversion
    review list (/admin/drafts) both publish through here. Returns (book id, work id,
    None) on success or (None, None, message) without importing anything.

    A multi-volume book is one file per volume, grouped into a work by identical (title,
    author): when `work_id` is given the volume is forced onto that work's own
    title/author/death label so it can never split off into a new work."""
    settings = get_settings()
    if not isinstance(content, dict):
        return None, None, "الملف ليس كتاباً بصيغة v2"
    metadata = content.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        return None, None, "حقل metadata غير صالح"

    forced_work: int | None = None
    replacing: int | None = None
    if replace_book_id.strip().isdigit():
        # Replacing a published book with a corrected version: the same id, so the work, the
        # volume and everything the apps saved against it stay; the old file is kept aside.
        replacing = int(replace_book_id)
        old = (await session.execute(text("""
            SELECT b.id, b.volume, b.work_id, b.page_count, w.title, a.name AS author, a.death_label
            FROM books b JOIN works w ON w.id = b.work_id LEFT JOIN authors a ON a.id = w.author_id
            WHERE b.id = :id"""), {"id": replacing})).first()
        if old is None:
            return None, None, f"الكتاب #{replacing} غير موجود"
        new_volume = int(volume) if volume.strip().isdigit() else (
            int(metadata["volume"]) if str(metadata.get("volume", "")).isdigit() else None)
        differences = []
        if normalize(content.get("title", "")) != normalize(old.title):
            differences.append(f"العنوان («{content.get('title', '')}» بدل «{old.title}»)")
        if normalize(content.get("author", "")) != normalize(old.author or ""):
            differences.append(f"المؤلف («{content.get('author', '')}» بدل «{old.author or ''}»)")
        if new_volume != old.volume:
            differences.append(f"المجلد ({new_volume or '—'} بدل {old.volume or '—'})")
        if differences and not confirm_mismatch:
            return None, None, ("يختلف عن الكتاب #%d في: %s -- أكّد الاستبدال إن كان مقصوداً"
                                % (replacing, "، ".join(differences)))
        # The book keeps its own identity whatever the draft says.
        forced_work = old.work_id
        content["title"] = old.title
        content["author"] = old.author or ""
        if old.death_label:
            metadata["authorDeath"] = old.death_label
        else:
            metadata.pop("authorDeath", None)
        if old.volume:
            metadata["volume"] = str(old.volume)
        else:
            metadata.pop("volume", None)
        volume, work_id = "", ""
        if report is not None:
            report["oldPages"] = old.page_count
    if work_id.strip().isdigit():
        row = (await session.execute(
            text("""
                SELECT w.id, w.title, a.name AS author, a.death_label
                FROM works w LEFT JOIN authors a ON a.id = w.author_id WHERE w.id = :id
            """),
            {"id": int(work_id)},
        )).first()
        if row is None:
            return None, None, "العمل غير موجود"
        forced_work = row.id
        content["title"] = row.title
        content["author"] = row.author or ""
        if row.death_label:
            metadata["authorDeath"] = row.death_label
        else:
            metadata.pop("authorDeath", None)
        if not volume.strip().isdigit():
            return None, None, "رقم المجلد مطلوب عند إضافة مجلد إلى عمل"

    if volume.strip().isdigit():
        metadata["volume"] = str(int(volume))

    resolved_id = replacing or (int(book_id) if book_id.strip().isdigit() else await _next_manual_id(session))
    content["bookId"] = str(resolved_id)

    importer = _importer()
    errors = [i for i in importer.val.validate(content) if i.severity == "error"]
    if errors:
        detail = "؛ ".join(f"{i.code}: {i.detail}" for i in errors[:4])
        return None, None, f"الملف غير صالح ({len(errors)} خطأ): {detail}"

    # Same (title, author) as an existing work means this file would join it: refuse a
    # second copy of the same volume, and refuse an unnumbered volume next to real ones.
    title_norm = normalize(content.get("title", ""))
    author_norm = normalize(content.get("author", ""))
    existing = (await session.execute(
        text("""
            SELECT b.id, b.volume FROM books b
            JOIN works w ON w.id = b.work_id
            LEFT JOIN authors a ON a.id = w.author_id
            WHERE w.title_norm = :tn AND COALESCE(a.name_norm, '') = :an AND b.id <> :me
        """),
        {"tn": title_norm, "an": author_norm, "me": resolved_id},
    )).all()
    vol_int = int(metadata["volume"]) if str(metadata.get("volume", "")).isdigit() else None
    if existing:
        if vol_int is None:
            return None, None, (
                "يوجد كتاب بالعنوان والمؤلف نفسيهما: حدّد رقم المجلد، أو استخدم «إضافة مجلد جديد» في صفحة العمل"
            )
        clash = next((r for r in existing if r.volume == vol_int), None)
        if clash:
            return None, None, f"المجلد {vol_int} موجود مسبقاً (كتاب #{clash.id})"

    if replacing is not None and report is not None:
        kept = settings.books_root / f"{replacing}.json"
        if kept.exists():  # the only copy of the old pages once the import replaces them
            aside = settings.books_root / REPLACED_DIR
            aside.mkdir(parents=True, exist_ok=True)
            name = f"{replacing}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.json"
            shutil.copy2(kept, aside / name)
            report["backup"] = name

    result = await importer._import_content(
        session, content, str(resolved_id), f"{resolved_id}.json", source,
        settings.books_root, True,
    )
    if result != "ok":
        return None, None, f"فشل الاستيراد ({result}) -- راجع سجل الاستيراد لمعرفة السبب"

    if report is not None:
        report["newPages"] = len(content.get("pages") or [])
    new_work = forced_work or await session.scalar(
        text("SELECT work_id FROM books WHERE id = :id"), {"id": resolved_id}
    )
    if subject and not forced_work:
        await _set_work_subjects(session, new_work, subject)
    if library and not forced_work:
        await _set_work_libraries(session, new_work, library)
    await session.commit()
    return resolved_id, new_work, None


@router.post("/books/new/manual")
async def new_book_manual(
    title: str = Form(...),
    author: str = Form(""),
    subject: list[str] = Form(...),
    language: str = Form("ar"),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_admin),
) -> RedirectResponse:
    author_id = await _get_or_create_author(session, author)
    work_id = await session.scalar(
        text("""
            INSERT INTO works (title, title_norm, author_id, language_code)
            VALUES (:title, :norm, :author, :lang)
            ON CONFLICT (title_norm, author_id) DO UPDATE SET title = EXCLUDED.title
            RETURNING id
        """),
        {"title": title, "norm": normalize(title), "author": author_id, "lang": language},
    )
    await _set_work_subjects(session, work_id, subject)
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


# ── Conversion review: books finished in the workbench, waiting to be published ─────
#
# The workbench (workbench/, its own container) writes drafts to WORKBENCH_ROOT; this
# panel reads the same folder. Publishing goes through _import_book_json, exactly like a
# JSON upload, then marks the draft published so the workbench shows it read-only.


# Where the old file of a replaced book is kept (inside books_root, below the *.json the
# importer and reindexer look at).
REPLACED_DIR = "_replaced"


def _drafts_root() -> Path:
    return get_settings().workbench_root


def _draft_or_404(draft_id: str) -> dict:
    try:
        return drafts.load(_drafts_root(), draft_id)
    except drafts.DraftNotFound:
        raise HTTPException(status_code=404, detail="Unknown draft") from None


_DRAFT_STATUS = {
    "editing": "قيد التحرير", "returned": "أُعيد للتعديل", "failed": "فشل التحويل",
    "submitted": "بانتظار المراجعة", "published": "منشور",
}


@router.get("/drafts", response_class=HTMLResponse)
async def drafts_list(
    ok: str | None = None, err: str | None = None, all: bool = False,
    session: AsyncSession = Depends(get_session), _: None = Depends(_require_admin),
) -> HTMLResponse:
    settings = get_settings()
    rows = drafts.list_all(_drafts_root())
    waiting = [r for r in rows if r["status"] == "submitted"]
    others = [r for r in rows if r["status"] != "submitted"] if all else []
    banner = _msg(ok, True) if ok else (_msg(err, False) if err else "")

    def row_html(r: dict) -> str:
        report = r.get("report") or {}
        link = f"{settings.workbench_url}/#/d/{r['id']}"
        published = (f' — <a href="/admin/books/{r["publishedBookId"]}">كتاب #{r["publishedBookId"]}</a>'
                     if r.get("publishedBookId") else "")
        return f"""<tr>
          <td><a href="/admin/drafts/{escape(r['id'])}">{escape(r.get('title') or r['sourceName'])}</a>
            <br><small>{escape(r['sourceName'])}</small></td>
          <td>{escape(r.get('author') or '—')}</td>
          <td>{report.get('pages', '—')}</td>
          <td>{_DRAFT_STATUS.get(r['status'], r['status'])}{published}</td>
          <td>{escape(r.get('submittedBy') or r.get('updatedBy') or '')}<br>
            <small>{escape((r.get('submittedAt') or r.get('updatedAt') or '')[:16].replace('T', ' '))}</small></td>
          <td><a href="{escape(link)}" target="_blank">فتح في المحوّل</a></td>
        </tr>"""

    table_head = "<tr><th>العنوان</th><th>المؤلف</th><th>الصفحات</th><th>الحالة</th><th>بواسطة</th><th></th></tr>"
    body = f"""{banner}<h1>مراجعة الكتب المحوّلة</h1>
    <p><small>كتب Word حُوّلت ودُققت في <a href="{escape(settings.workbench_url)}" target="_blank">المحوّل</a>
    وأُرسلت للمراجعة. افتح الكتاب للمراجعة ثم انشره في المكتبة، أو أعده للموظف مع ملاحظة.</small></p>
    <h2>بانتظار المراجعة ({len(waiting)})</h2>
    <table>{table_head}{''.join(row_html(r) for r in waiting) or '<tr><td colspan="6">لا توجد كتب بانتظار المراجعة.</td></tr>'}</table>
    <p><a href="/admin/drafts?all={'false' if all else 'true'}">{'إخفاء' if all else 'إظهار'} بقية الكتب (قيد التحرير والمنشورة)</a></p>
    {f"<table>{table_head}{''.join(row_html(r) for r in others)}</table>" if all else ""}"""
    return _render("مراجعة الكتب المحوّلة", body)


@router.get("/drafts/{draft_id}", response_class=HTMLResponse)
async def draft_detail(
    draft_id: str, ok: str | None = None, err: str | None = None, replace: str | None = None,
    session: AsyncSession = Depends(get_session), _: None = Depends(_require_admin),
) -> HTMLResponse:
    settings = get_settings()
    meta = _draft_or_404(draft_id)
    book = drafts.load_book(_drafts_root(), draft_id) or {}
    md = book.get("metadata") or {}
    report = meta.get("report") or {}
    issues = meta.get("issues") or []
    banner = _msg(ok, True) if ok else (_msg(err, False) if err else "")
    options = await _subject_checkboxes(session, set())
    library_options = await _library_checkboxes(session, set())

    toc = book.get("toc") or []
    toc_html = "".join(f"<li>{escape(e.get('title', ''))} <small>(ص {escape(str(e.get('pageNumber', '')))})</small></li>"
                       for e in toc[:40])
    if len(toc) > 40:
        toc_html += f"<li><small>… و{len(toc) - 40} عنواناً آخر</small></li>"
    issues_html = "".join(
        f"<li>{'خطأ' if i['severity'] == 'error' else 'تنبيه'} — {escape(i['code'])}: {escape(i['detail'])}</li>"
        for i in issues) or "<li>لا أخطاء ولا تنبيهات.</li>"
    meta_rows = "".join(f"<tr><th>{escape(k)}</th><td>{escape(str(v))}</td></tr>" for k, v in md.items())

    replace_id = (replace or "").strip()
    if not replace_id:  # the employee's note may name it: "يستبدل #900011"
        found = re.search(r"#\s*(\d+)", meta.get("submitNote") or "")
        replace_id = found.group(1) if found else ""
    replace_html = ""
    if replace_id.isdigit():
        old = (await session.execute(text("""
            SELECT b.id, b.volume, b.page_count, w.title, a.name AS author
            FROM books b JOIN works w ON w.id = b.work_id LEFT JOIN authors a ON a.id = w.author_id
            WHERE b.id = :id"""), {"id": int(replace_id)})).first()
        if old is None:
            replace_html = f"<p>لا يوجد كتاب برقم {escape(replace_id)}.</p>"
        else:
            new_volume = md.get("volume") or "—"
            def cell(a, b):
                return f"<td>{escape(str(a))}</td><td>{escape(str(b))}</td><td>{'' if str(a) == str(b) else '⚠'}</td>"
            differs = (normalize(book.get("title") or "") != normalize(old.title)
                       or normalize(book.get("author") or "") != normalize(old.author or "")
                       or str(new_volume) != str(old.volume or "—"))
            replace_html = f"""<table><tr><th></th><th>المنشور #{old.id}</th><th>هذه النسخة</th><th></th></tr>
              <tr><th>العنوان</th>{cell(old.title, book.get('title') or '')}</tr>
              <tr><th>المؤلف</th>{cell(old.author or '—', book.get('author') or '—')}</tr>
              <tr><th>المجلد</th>{cell(old.volume or '—', new_volume)}</tr>
              <tr><th>الصفحات</th>{cell(old.page_count, report.get('pages', '—'))}</tr></table>
            <p><small>تُحفظ نسخة الكتاب الحالي قبل الاستبدال ويمكن التراجع عنه. يبقى رقم الكتاب والعمل
            والتصنيف كما هي.</small></p>
            <form method="post" action="/admin/drafts/{escape(draft_id)}/publish">
              <input type="hidden" name="replace_book_id" value="{old.id}">
              {'<div class="row"><label><input type="checkbox" name="confirm" value="1"> أؤكد الاستبدال رغم الاختلاف المشار إليه (يبقى عنوان الكتاب ومؤلفه ومجلده المنشورة)</label></div>' if differs else ''}
              <button type="submit">استبدال الكتاب #{old.id}</button></form>"""
    actions = ""
    if meta["status"] == "submitted":
        actions = f"""
        <h2>نشر في المكتبة</h2>
        <form method="post" action="/admin/drafts/{escape(draft_id)}/publish">
          <div class="row"><label>رقم المجلد (اختياري -- للكتب متعددة المجلدات)</label>
            <input name="volume" type="number" min="1" value="{escape(str(md.get('volume', '')))}"></div>
          <div class="row"><label>إضافته كمجلد إلى عمل موجود (رقم العمل، اختياري)</label>
            <input name="work_id" type="number"></div>
          <div class="row"><label>التصنيف</label><div class="checkbox-group">{options}</div></div>
          <div class="row"><label>المكتبات (اختياري)</label><div class="checkbox-group">{library_options}</div></div>
          <button type="submit">نشر الكتاب</button>
        </form>
        <h2>استبدال كتاب منشور بهذه النسخة المصححة</h2>
        <form method="get" action="/admin/drafts/{escape(draft_id)}">
          <div class="row"><label>رقم الكتاب المنشور الذي يُستبدل</label>
            <input name="replace" type="number" min="1" value="{escape(replace_id)}" required></div>
          <button type="submit" style="background:#555">مقارنة</button>
        </form>
        {replace_html}
        <h2>إعادة للموظف</h2>
        <form method="post" action="/admin/drafts/{escape(draft_id)}/return">
          <div class="row"><label>ما الذي يجب تصحيحه؟</label><textarea name="note" rows="3" required></textarea></div>
          <button type="submit" style="background:#8a6d3b">إعادة للتعديل</button>
        </form>"""
    elif meta["status"] == "published":
        actions = f'<p>نُشر ككتاب <a href="/admin/books/{meta["publishedBookId"]}">#{meta["publishedBookId"]}</a>.</p>'
        if meta.get("replacedBackup"):
            actions += f"""<p>استبدل نسخة سابقة ({meta.get('replacedOldPages', '—')} صفحة ← {meta.get('replacedNewPages', '—')}).
            النسخة السابقة محفوظة.</p>
            <form method="post" action="/admin/drafts/{escape(draft_id)}/undo-replace"
                  onsubmit="return confirm('إعادة النسخة السابقة؟ تُستبدل الصفحات الحالية بها.')">
              <button type="submit" style="background:#8a6d3b">تراجع: إعادة النسخة السابقة</button></form>"""


    body = f"""{banner}
    <p><a href="/admin/drafts">&rarr; قائمة المراجعة</a></p>
    <h1>{escape(book.get('title') or meta.get('title') or '')}</h1>
    <table>
      <tr><th>المؤلف</th><td>{escape(book.get('author') or '—')}</td></tr>
      <tr><th>الحالة</th><td>{_DRAFT_STATUS.get(meta['status'], meta['status'])}</td></tr>
      <tr><th>الملف الأصلي</th><td>{escape(meta['sourceName'])}</td></tr>
      <tr><th>الصفحات</th><td>{report.get('pages', '—')} ({report.get('frontPages', 0)} مقدمة) —
        الأرقام المطبوعة {escape(str(report.get('printedFirst', '')))} … {escape(str(report.get('printedLast', '')))}</td></tr>
      <tr><th>العناوين</th><td>{len(toc)}</td></tr>
      <tr><th>أرسله</th><td>{escape(meta.get('submittedBy') or '—')} {escape((meta.get('submittedAt') or '')[:16].replace('T', ' '))}</td></tr>
      <tr><th>ملاحظة الموظف</th><td>{escape(meta.get('submitNote') or '—')}</td></tr>
      {meta_rows}
    </table>
    <p><a href="{escape(settings.workbench_url)}/#/d/{escape(draft_id)}" target="_blank">فتح في المحوّل (مقارنة الصفحات بالأصل)</a>
      &nbsp;|&nbsp; <a href="/admin/drafts/{escape(draft_id)}/book.json">تنزيل JSON</a></p>
    <h2>التحقق</h2><ul>{issues_html}</ul>
    <h2>الفهرس</h2><ol>{toc_html or '<li>لا توجد عناوين</li>'}</ol>
    {actions}"""
    return _render("مراجعة كتاب", body)


@router.get("/drafts/{draft_id}/book.json")
async def draft_book_json(draft_id: str, _: None = Depends(_require_admin)) -> Response:
    meta = _draft_or_404(draft_id)
    path = drafts.draft_dir(_drafts_root(), draft_id) / "book.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not converted")
    return FileResponse(path, media_type="application/json",
                        filename=drafts.book_filename(meta.get("title", ""), draft_id))


@router.post("/drafts/{draft_id}/publish")
async def draft_publish(
    draft_id: str,
    volume: str = Form(""),
    work_id: str = Form(""),
    subject: list[str] = Form([]),
    library: list[int] = Form([]),
    replace_book_id: str = Form(""),
    confirm: str = Form(""),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_admin),
    credentials: HTTPBasicCredentials = Depends(_security),
) -> RedirectResponse:
    meta = _draft_or_404(draft_id)
    back = f"/admin/drafts/{draft_id}"
    if meta["status"] != "submitted":
        return RedirectResponse(f"{back}?err=الكتاب ليس بانتظار المراجعة", status_code=303)
    book = drafts.load_book(_drafts_root(), draft_id)
    if book is None:
        return RedirectResponse(f"{back}?err=لا يوجد ملف كتاب لهذه المسودة", status_code=303)
    replaced: dict = {}
    new_book, new_work, error = await _import_book_json(
        session, book, volume=volume, work_id=work_id, subject=subject, library=library,
        source="workbench-replace" if replace_book_id.strip() else "workbench",
        replace_book_id=replace_book_id, confirm_mismatch=bool(confirm), report=replaced,
    )
    if error:
        return RedirectResponse(f"{back}?err={error}", status_code=303)
    extra = {}
    if replace_book_id.strip():
        extra = {"replacedBackup": replaced.get("backup"), "replacedOldPages": replaced.get("oldPages"),
                 "replacedNewPages": replaced.get("newPages")}
    drafts.update(_drafts_root(), draft_id, status="published", publishedBookId=new_book,
                  publishedAt=drafts.now(), publishedBy=credentials.username, **extra)
    done = (f"تم استبدال الكتاب #{new_book}: {replaced.get('oldPages', '—')} صفحة ← {replaced.get('newPages')} صفحة"
            if replace_book_id.strip() else f"تم نشر الكتاب رقم {new_book} من المحوّل")
    return RedirectResponse(f"/admin/works/{new_work}?ok={done}", status_code=303)


@router.post("/drafts/{draft_id}/undo-replace")
async def draft_undo_replace(
    draft_id: str, session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_admin),
) -> RedirectResponse:
    """Put a replaced book's old pages back (from the file kept when it was replaced)."""
    meta = _draft_or_404(draft_id)
    back = f"/admin/drafts/{draft_id}"
    if meta["status"] != "published" or not meta.get("replacedBackup"):
        return RedirectResponse(f"{back}?err=لا توجد نسخة سابقة لهذا الكتاب", status_code=303)
    aside = get_settings().books_root / REPLACED_DIR / Path(meta["replacedBackup"]).name
    if not aside.exists():
        return RedirectResponse(f"{back}?err=ملف النسخة السابقة غير موجود", status_code=303)
    restored: dict = {}
    book_id, work, error = await _import_book_json(
        session, json.loads(aside.read_text(encoding="utf-8")), replace_book_id=str(meta["publishedBookId"]),
        confirm_mismatch=True, source="workbench-undo", report=restored,
    )
    if error:
        return RedirectResponse(f"{back}?err={error}", status_code=303)
    drafts.update(_drafts_root(), draft_id, status="submitted", replacedBackup=None,
                  restoredAt=drafts.now())
    return RedirectResponse(f"/admin/works/{work}?ok=أُعيدت النسخة السابقة للكتاب #{book_id}", status_code=303)


@router.post("/drafts/{draft_id}/return")
async def draft_return(
    draft_id: str, note: str = Form(...), _: None = Depends(_require_admin),
    credentials: HTTPBasicCredentials = Depends(_security),
) -> RedirectResponse:
    meta = _draft_or_404(draft_id)
    if meta["status"] != "submitted":
        return RedirectResponse(f"/admin/drafts/{draft_id}?err=الكتاب ليس بانتظار المراجعة", status_code=303)
    drafts.update(_drafts_root(), draft_id, status="returned", reviewNote=note.strip(),
                  reviewedBy=credentials.username, reviewedAt=drafts.now())
    return RedirectResponse("/admin/drafts?ok=أُعيد الكتاب للموظف مع الملاحظة", status_code=303)
