"""The conversion workbench: a small web app for converting Word books to the library's
v2 JSON and checking them page by page before they are published.

An employee uploads a .doc/.docx (plus the download's readme.txt if there is one); the
server converts it with scripts/convert/doc_to_json_v2.py and renders the original with
LibreOffice. The page (static/index.html) shows each original page next to its converted
page, lets them fix text, headings, page numbers and metadata, re-run the conversion with
different options, and finally submit the book -- which puts it on the admin panel's
review list (/admin/drafts), where it is published into the library.

Runs as its own container (Dockerfile.workbench: LibreOffice + Arabic fonts), apart from
the API, sharing only the drafts folder. See app/services/drafts.py for storage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.services import drafts
from workbench import conversion

log = logging.getLogger("workbench")

STATIC = Path(__file__).resolve().parent / "static"
MAX_UPLOAD_BYTES = 150 * 1024 * 1024


class WorkbenchSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=Path(__file__).resolve().parents[1] / ".env",
                                      env_file_encoding="utf-8", extra="ignore")

    workbench_root: Path = Path("data/workbench")
    # "name:password,name2:password2" -- the people allowed to use the workbench.
    workbench_users: str = ""


@lru_cache
def get_settings() -> WorkbenchSettings:
    return WorkbenchSettings()


def root() -> Path:
    path = get_settings().workbench_root
    path.mkdir(parents=True, exist_ok=True)
    return path


_security = HTTPBasic(realm="Thaqalayn workbench")


def current_user(credentials: HTTPBasicCredentials = Depends(_security)) -> str:
    users = {}
    for pair in get_settings().workbench_users.split(","):
        name, _, password = pair.strip().partition(":")
        if name and password:
            users[name] = password
    expected = users.get(credentials.username)
    if expected is None or not secrets.compare_digest(credentials.password, expected):
        raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


app = FastAPI(title="Thaqalayn conversion workbench", docs_url=None, redoc_url=None)


def _meta_or_404(draft_id: str) -> dict:
    try:
        return drafts.load(root(), draft_id)
    except drafts.DraftNotFound:
        raise HTTPException(404, "لا توجد مسودة بهذا الرقم") from None


PLAN_STALE_AFTER = timedelta(minutes=30)  # a job the server lost (a restart) must not lock the draft


def _plan_running(meta: dict) -> bool:
    plan = meta.get("plan") or {}
    if plan.get("status") not in ("running", "converting"):
        return False
    try:
        return datetime.now(timezone.utc) - datetime.fromisoformat(plan["startedAt"]) < PLAN_STALE_AFTER
    except (KeyError, ValueError):
        return False


def _plan_state(status: str, **more) -> dict:
    return {"status": status, "message": None, "startedAt": datetime.now(timezone.utc).isoformat(), **more}


def _editable_or_409(meta: dict) -> None:
    if meta["status"] not in drafts.EDITABLE:
        raise HTTPException(409, "المسودة مُرسلة للمراجعة أو منشورة، ولا يمكن تعديلها الآن")
    if _plan_running(meta):
        raise HTTPException(409, "جاري اشتقاق صفحات الكتاب من ملف PDF؛ انتظر حتى ينتهي")


def _run_conversion(draft_id: str, user: str, keep: dict | None) -> dict:
    folder = drafts.draft_dir(root(), draft_id)
    meta = drafts.load(root(), draft_id)
    try:
        content, report, issues = conversion.convert(folder, meta, keep)
    except Exception as exc:  # noqa: BLE001 -- any converter failure is reported, not a 500
        log.exception("conversion of %s failed", draft_id)
        return drafts.update(root(), draft_id, status="failed", error=f"{type(exc).__name__}: {exc}",
                             updatedBy=user, updatedAt=drafts.now())
    drafts.save_book(root(), draft_id, content)
    return drafts.update(
        root(), draft_id, status="editing" if meta["status"] == "failed" else meta["status"],
        error=None, report=report, issues=issues, title=content["title"], author=content["author"],
        updatedBy=user, updatedAt=drafts.now(),
    )


def _plan_job(draft_id: str, user: str) -> None:
    """The printed PDF's pages: made into a page plan, then the book is converted again with
    it. The employee cannot edit meanwhile (a conversion replaces the pages)."""
    folder = drafts.draft_dir(root(), draft_id)
    meta = drafts.load(root(), draft_id)
    drafts.update(root(), draft_id, plan=_plan_state("running"))
    try:
        summary = conversion.make_page_plan(folder, meta["sourceFile"])
    except Exception as exc:  # noqa: BLE001 -- the Word-layout conversion stays as it was
        log.exception("page plan of %s failed", draft_id)
        (folder / conversion.PAGE_PLAN).unlink(missing_ok=True)
        drafts.update(root(), draft_id, plan={"status": "failed", "message": str(exc)[:300]})
        return
    drafts.update(root(), draft_id, plan=_plan_state("converting", **summary))
    keep = drafts.load_book(root(), draft_id)
    result = _run_conversion(draft_id, user, keep)
    if result.get("status") == "failed":
        (folder / conversion.PAGE_PLAN).unlink(missing_ok=True)
        drafts.update(root(), draft_id, plan={"status": "failed", "message": result.get("error"), **summary})
        return
    drafts.update(root(), draft_id, plan={"status": "done", "message": None, **summary})


def _render(draft_id: str) -> None:
    """The original to compare against, rendered from the Word file by LibreOffice."""
    folder = drafts.draft_dir(root(), draft_id)
    meta = drafts.load(root(), draft_id)
    word = {"source": "word", "pdfName": None}
    if not conversion.renderer_available():
        drafts.update(root(), draft_id, render={"status": "unavailable", "pages": 0,
                                                "error": "LibreOffice is not installed here", **word})
        return
    drafts.update(root(), draft_id, render={"status": "running", "pages": 0, "error": None, **word})
    try:
        pages = conversion.render_pdf(folder, meta["sourceFile"])
        conversion.extract_original_words(folder, pages)
        drafts.update(root(), draft_id, render={"status": "done", "pages": pages, "error": None, **word})
    except Exception as exc:  # noqa: BLE001
        log.exception("rendering %s failed", draft_id)
        drafts.update(root(), draft_id, render={"status": "failed", "pages": 0, "error": str(exc)[:300], **word})


def _index_pdf(draft_id: str, pages: int, name: str) -> None:
    """After an uploaded PDF replaced the original: read its words for page matching."""
    pdf = {"source": "pdf", "pdfName": name}
    try:
        conversion.extract_original_words(drafts.draft_dir(root(), draft_id), pages)
        drafts.update(root(), draft_id, render={"status": "done", "pages": pages, "error": None, **pdf})
    except Exception as exc:  # noqa: BLE001
        log.exception("reading PDF of %s failed", draft_id)
        drafts.update(root(), draft_id, render={"status": "failed", "pages": 0, "error": str(exc)[:300], **pdf})


async def _accept_pdf(draft_id: str, upload: UploadFile, background: BackgroundTasks) -> dict:
    name = Path(upload.filename or "original.pdf").name
    data = await upload.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "ملف PDF أكبر من المسموح (150 ميغابايت)")
    try:
        pages = await asyncio.to_thread(conversion.use_pdf, drafts.draft_dir(root(), draft_id), data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    background.add_task(_index_pdf, draft_id, pages, name)
    return drafts.update(root(), draft_id, render={"status": "running", "pages": pages, "error": None,
                                                    "source": "pdf", "pdfName": name})


# ── pages ───────────────────────────────────────────────────────────────────


@app.get("/", include_in_schema=False)
def index(_: str = Depends(current_user)) -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/health", include_in_schema=False)
def health() -> dict:
    return {"status": "ok", "renderer": conversion.renderer_available()}


# ── drafts API ──────────────────────────────────────────────────────────────


@app.get("/api/me")
def me(user: str = Depends(current_user)) -> dict:
    return {"user": user, "renderer": conversion.renderer_available()}


async def _read_page_plan(upload: UploadFile | None) -> bytes | None:
    """pages.json from scripts/convert/pdf_pages.py: where the printed PDF's pages begin."""
    if upload is None or not upload.filename:
        return None
    data = await upload.read()
    try:
        plan = json.loads(data)
        ok = plan["version"] == 1 and all("para" in pg and "chunk" in pg and "words" in pg
                                          for pg in plan["pages"]) and plan["pages"]
    except (ValueError, KeyError, TypeError):
        ok = False
    if not ok:
        raise HTTPException(400, "ملف الصفحات (pages.json) غير صالح: أنشئه بالأداة pdf_pages.py")
    return data


@app.get("/api/drafts")
def list_drafts(_: str = Depends(current_user)) -> list[dict]:
    return drafts.list_all(root())


@app.post("/api/drafts")
async def create_draft(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    readme: UploadFile | None = File(None),
    pdf: UploadFile | None = File(None),
    pages: UploadFile | None = File(None),
    user: str = Depends(current_user),
) -> dict:
    name = Path(file.filename or "").name
    if not name.lower().endswith((".doc", ".docx")):
        raise HTTPException(400, "الملف يجب أن يكون بصيغة Word ‏(.doc أو ‎.docx)")
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "الملف أكبر من المسموح (150 ميغابايت)")
    readme_bytes = await readme.read() if readme is not None and readme.filename else None
    plan = await _read_page_plan(pages)
    meta = drafts.create(root(), source_name=name, source_bytes=data, created_by=user,
                         readme_bytes=readme_bytes)
    if plan is not None:
        (drafts.draft_dir(root(), meta["id"]) / conversion.PAGE_PLAN).write_bytes(plan)
    meta = await asyncio.to_thread(_run_conversion, meta["id"], user, None)
    if pdf is not None and pdf.filename:
        try:
            accepted = await _accept_pdf(meta["id"], pdf, background)
            if plan is None:  # no pages.json given: the server makes the plan from the PDF
                accepted = drafts.update(root(), meta["id"], plan=_plan_state("running"))
                background.add_task(_plan_job, meta["id"], user)
            return accepted
        except HTTPException:
            pass  # a bad PDF doesn't lose the upload: fall back to rendering the Word file
    background.add_task(_render, meta["id"])
    return meta


@app.get("/api/drafts/{draft_id}")
def get_draft(draft_id: str, _: str = Depends(current_user)) -> dict:
    return _meta_or_404(draft_id)


@app.get("/api/drafts/{draft_id}/book")
def get_book(draft_id: str, _: str = Depends(current_user)) -> JSONResponse:
    _meta_or_404(draft_id)
    book = drafts.load_book(root(), draft_id)
    if book is None:
        raise HTTPException(404, "لم يتم تحويل هذا الكتاب بعد")
    return JSONResponse(book, headers={"Cache-Control": "no-store"})


@app.put("/api/drafts/{draft_id}/book")
async def save_book(draft_id: str, book: dict, user: str = Depends(current_user)) -> dict:
    """Save the employee's edited book. Validation problems are returned, not refused:
    a half-fixed book must still be savable; only submitting requires it to be valid."""
    meta = _meta_or_404(draft_id)
    _editable_or_409(meta)
    if book.get("schemaVersion") != 2 or not isinstance(book.get("pages"), list):
        raise HTTPException(400, "ليس كتاباً بصيغة v2")
    book["bookId"] = "900001"
    issues = await asyncio.to_thread(conversion.issues_of, book)
    issues += (meta.get("report") or {}).get("sourceIssues", [])  # the Word file's own, from conversion
    drafts.save_book(root(), draft_id, book)
    report = dict(meta.get("report") or {}, pages=len(book["pages"]), tocEntries=len(book.get("toc", [])))
    return drafts.update(root(), draft_id, issues=issues, title=book.get("title", ""),
                         author=book.get("author", ""), report=report,
                         updatedBy=user, updatedAt=drafts.now())


@app.post("/api/drafts/{draft_id}/reconvert")
async def reconvert(draft_id: str, options: dict, user: str = Depends(current_user)) -> dict:
    """Convert again with different options (blank pages, front matter, headings...).
    Page edits are replaced; title, author and metadata are kept."""
    meta = _meta_or_404(draft_id)
    _editable_or_409(meta)
    drafts.update(root(), draft_id, options=conversion.clean_options(options))
    keep = drafts.load_book(root(), draft_id)
    return await asyncio.to_thread(_run_conversion, draft_id, user, keep)


@app.post("/api/drafts/{draft_id}/render")
def rerender(draft_id: str, background: BackgroundTasks, _: str = Depends(current_user)) -> dict:
    """Compare against the Word file rendered by LibreOffice (again, or instead of a PDF)."""
    meta = _meta_or_404(draft_id)
    if meta["render"]["status"] == "running":
        return meta
    background.add_task(_render, draft_id)
    return drafts.update(root(), draft_id, render={"status": "pending", "pages": 0, "error": None,
                                                    "source": "word", "pdfName": None})


@app.post("/api/drafts/{draft_id}/plan")
def make_plan_from_pdf(draft_id: str, background: BackgroundTasks, user: str = Depends(current_user)) -> dict:
    """Take the book's pages from the uploaded printed PDF (the draft's original)."""
    meta = _meta_or_404(draft_id)
    if meta["status"] not in drafts.EDITABLE:
        raise HTTPException(409, "المسودة مُرسلة للمراجعة أو منشورة، ولا يمكن تعديلها الآن")
    if _plan_running(meta):
        return meta
    if (meta.get("render") or {}).get("source") != "pdf":
        raise HTTPException(409, "ارفع ملف PDF للكتاب المطبوع أولاً")
    background.add_task(_plan_job, draft_id, user)
    return drafts.update(root(), draft_id, plan=_plan_state("running"))


@app.post("/api/drafts/{draft_id}/pdf")
async def upload_pdf(draft_id: str, background: BackgroundTasks, file: UploadFile = File(...),
                     _: str = Depends(current_user)) -> dict:
    """Compare against an uploaded PDF instead: best is one saved from Word itself."""
    meta = _meta_or_404(draft_id)
    if meta["render"]["status"] == "running":
        raise HTTPException(409, "الأصل قيد التجهيز، حاول بعد قليل")
    return await _accept_pdf(draft_id, file, background)


@app.get("/api/drafts/{draft_id}/original/{number}")
async def original_page(draft_id: str, number: int, _: str = Depends(current_user)) -> FileResponse:
    meta = _meta_or_404(draft_id)
    if meta["render"]["status"] != "done" or not 1 <= number <= meta["render"]["pages"]:
        raise HTTPException(404, "صفحة غير متوفرة")
    path = await asyncio.to_thread(conversion.page_image, drafts.draft_dir(root(), draft_id), number)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/drafts/{draft_id}/pagemap")
async def get_page_map(draft_id: str, _: str = Depends(current_user)) -> dict:
    """Which rendered original pages each converted page spans -- computed against the
    book as last saved, so it follows page edits (merges, splits, deletions)."""
    meta = _meta_or_404(draft_id)
    folder = drafts.draft_dir(root(), draft_id)
    words_file = folder / "original_words.json"
    book = drafts.load_book(root(), draft_id)
    if meta["render"]["status"] != "done" or book is None:
        return {"pages": []}
    if not words_file.exists():  # rendered before page maps existed
        await asyncio.to_thread(conversion.extract_original_words, folder, meta["render"]["pages"])
    pdf_words = json.loads(words_file.read_text(encoding="utf-8"))
    return {"pages": await asyncio.to_thread(conversion.page_map, book, pdf_words)}


@app.get("/api/drafts/{draft_id}/source")
def download_source(draft_id: str, _: str = Depends(current_user)) -> FileResponse:
    meta = _meta_or_404(draft_id)
    return FileResponse(drafts.draft_dir(root(), draft_id) / meta["sourceFile"], filename=meta["sourceName"])


@app.get("/api/drafts/{draft_id}/download")
def download_book(draft_id: str, _: str = Depends(current_user)) -> Response:
    meta = _meta_or_404(draft_id)
    path = drafts.draft_dir(root(), draft_id) / "book.json"
    if not path.exists():
        raise HTTPException(404, "لم يتم تحويل هذا الكتاب بعد")
    return FileResponse(path, media_type="application/json",
                        filename=drafts.book_filename(meta.get("title", ""), draft_id))


@app.post("/api/drafts/{draft_id}/submit")
def submit(draft_id: str, note: str = Form(""), user: str = Depends(current_user)) -> dict:
    meta = _meta_or_404(draft_id)
    _editable_or_409(meta)
    book = drafts.load_book(root(), draft_id)
    if book is None:
        raise HTTPException(409, "لم يتم تحويل هذا الكتاب بعد")
    issues = conversion.issues_of(book) + (meta.get("report") or {}).get("sourceIssues", [])
    problems = [i["detail"] for i in issues if i["severity"] == "error"]
    if not (book.get("title") or "").strip():
        problems.insert(0, "عنوان الكتاب فارغ")
    if not (book.get("author") or "").strip():
        problems.insert(0, "اسم المؤلف فارغ")
    if problems:
        raise HTTPException(422, {"message": "لا يمكن الإرسال قبل إصلاح هذه الأخطاء", "problems": problems[:20]})
    return drafts.update(root(), draft_id, status="submitted", submitNote=note.strip() or None,
                         submittedBy=user, submittedAt=drafts.now(), updatedBy=user, updatedAt=drafts.now())


@app.post("/api/drafts/{draft_id}/withdraw")
def withdraw(draft_id: str, user: str = Depends(current_user)) -> dict:
    """Take a submitted book back for more edits, before it is published."""
    meta = _meta_or_404(draft_id)
    if meta["status"] != "submitted":
        raise HTTPException(409, "المسودة ليست بانتظار المراجعة")
    return drafts.update(root(), draft_id, status="editing", updatedBy=user, updatedAt=drafts.now())


@app.delete("/api/drafts/{draft_id}")
def delete_draft(draft_id: str, _: str = Depends(current_user)) -> dict:
    meta = _meta_or_404(draft_id)
    if meta["status"] == "published":
        raise HTTPException(409, "لا يمكن حذف كتاب منشور من هنا")
    drafts.delete(root(), draft_id)
    return {"deleted": draft_id}
