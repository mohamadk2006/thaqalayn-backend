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
import secrets
import sys
from html import escape
from pathlib import Path

import docx
from docx.oxml.ns import qn
from fastapi import APIRouter, Depends, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.exceptions import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.books import _resolve_under_root
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
  .checkbox-group {{ max-height: 14rem; overflow-y: auto; border: 1px solid #ddd;
    padding: 0.4rem 0.6rem; background: #fafafa; }}
  .checkbox-row {{ display: block; font-weight: normal; margin: 0.2rem 0; }}
  .checkbox-row input {{ width: auto; margin-left: 0.5rem; }}
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
        '<p><a href="/admin/featured">الكتب المختارة &rarr;</a></p>'
        "<h1>التصنيفات</h1>"
        "<table><tr><th>التصنيف</th><th>عدد العناوين</th><th>عدد المجلدات</th></tr>"
        f"{rows_html}</table>"
    )
    return _render("لوحة التحكم", body)


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
        GROUP BY w.id, w.title, a.name
        ORDER BY w.title_norm
    """))).all()
    rows_html = "".join(
        f'<tr><td><a href="/admin/works/{r.work_id}">{escape(r.title)}</a></td>'
        f"<td>{escape(r.author or '')}</td><td>{escape(r.subject_titles or '')}</td></tr>"
        for r in rows
    )
    body = (
        f"<h1>الكتب المختارة ({len(rows)})</h1>"
        "<table><tr><th>العنوان</th><th>المؤلف</th><th>التصنيف</th></tr>"
        f"{rows_html}</table>"
    )
    return _render("الكتب المختارة", body)


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
        f'<td><a href="/admin/books/{r.id}">تعديل</a> | '
        f'<a href="/admin/books/{r.id}/content">عرض المحتوى</a></td></tr>'
        for r in rows
    )
    body = (
        f"<h1>{escape(work.title)}</h1>"
        "<table><tr><th>المجلد</th><th>العنوان</th><th>الحالة</th><th>الصفحات</th><th></th></tr>"
        f"{rows_html}</table>"
    )
    return _render(work.title, body)


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
    await session.execute(
        text("UPDATE works SET is_featured = :feat WHERE id = :wid"),
        {"feat": is_featured, "wid": exists},
    )
    await _set_work_subjects(session, exists, subject)
    await session.commit()
    return RedirectResponse(f"/admin/books/{book_id}?saved=1", status_code=303)


# ── Book content viewer ──────────────────────────────────────────────────────
#
# Reads the same JSON file the download endpoint serves and renders it the way it's
# actually organized -- chapter, then section (a فهرس الموضوعات heading), then that
# section's paragraphs with page numbers -- rather than a raw JSON dump. Sections are
# the pagination unit here (not the whole book): the largest real files are 10-14 MB
# with dozens of sections, and there's no reason to ever build all of that into one
# HTML response when a book is opened.


def _flat_sections(content: dict) -> list[dict]:
    """Every v2 TOC entry, in reading order, each carrying the pages it covers -- a
    section's range runs from its own page to just before the next entry's page (or the
    book's last page, for the final entry), same logic as paginate_sections() in
    app/services/paging.py. A source with no فهرس الموضوعات at all (no toc entries)
    becomes a single synthetic section spanning every page, so the viewer still has
    something to open instead of an empty index -- mirrors how a Section-less Page
    already works for search."""
    pages = sorted(content.get("pages", []), key=lambda p: p.get("sequence", 0))
    if not pages:
        return []

    page_by_id = {p["id"]: p for p in pages}
    toc = sorted(content.get("toc", []), key=lambda e: e.get("order", 0))
    if not toc:
        return [{"title": None, "pages": pages}]

    flat = []
    for i, entry in enumerate(toc):
        start_page = page_by_id.get(entry.get("pageId"))
        start_seq = start_page["sequence"] if start_page else None
        if start_seq is None:
            flat.append({"title": entry.get("title"), "pages": []})
            continue
        if i + 1 < len(toc):
            next_page = page_by_id.get(toc[i + 1].get("pageId"))
            end_seq = (next_page["sequence"] - 1) if next_page else start_seq
        else:
            end_seq = pages[-1]["sequence"]
        flat.append({
            "title": entry.get("title"),
            "pages": [p for p in pages if start_seq <= p["sequence"] <= end_seq],
        })
    return flat


@router.get("/books/{book_id:int}/content", response_class=HTMLResponse)
async def book_content(
    book_id: int,
    section: int | None = None,
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
    sections = _flat_sections(content)

    if section is None:
        rows_html = "".join(
            f'<tr><td>{i + 1}</td><td>{escape(s["title"] or "(الكتاب كاملاً)")}</td>'
            f'<td><a href="?section={i}">فتح</a></td></tr>'
            for i, s in enumerate(sections)
        )
        body = (
            f"<h1>{escape(row.title)}</h1>"
            f"<p><small>{escape(content.get('author') or '')}</small></p>"
            "<table><tr><th>#</th><th>العنوان</th><th></th></tr>"
            f"{rows_html}</table>"
            f'<p><a href="/admin/books/{book_id}">&larr; رجوع للتعديل</a></p>'
        )
        return _render(row.title, body)

    if not (0 <= section < len(sections)):
        raise HTTPException(status_code=404, detail="Unknown section")

    sec = sections[section]
    blocks_html = []
    current_page_number: str | None = None
    for page in sec["pages"]:
        if page["pageNumber"] != current_page_number:
            current_page_number = page["pageNumber"]
            blocks_html.append(f'<p><small>-- صفحة {escape(current_page_number)} --</small></p>')
        for block in sorted(page.get("blocks", []), key=lambda b: b.get("order", 0)):
            text_ = escape(block.get("text") or "")
            if block.get("type") == "heading":
                blocks_html.append(f"<p><strong>{text_}</strong></p>")
            elif block.get("type") == "footnotes":
                blocks_html.append(f"<p><small>{text_}</small></p>")
            else:
                blocks_html.append(f"<p>{text_}</p>")

    nav = (
        (f'<a href="?section={section - 1}">&larr; السابق</a>' if section > 0 else "")
        + " &nbsp;|&nbsp; "
        + f'<a href="/admin/books/{book_id}/content">الفهرس</a>'
        + " &nbsp;|&nbsp; "
        + (f'<a href="?section={section + 1}">التالي &rarr;</a>'
           if section + 1 < len(sections) else "")
    )
    title = sec["title"] or row.title
    body = (
        f"<h1>{escape(title)}</h1>"
        f"<p>{nav}</p>"
        f"{''.join(blocks_html)}"
        f"<p>{nav}</p>"
    )
    return _render(title, body)


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
