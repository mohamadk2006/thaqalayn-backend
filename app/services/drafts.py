"""Conversion drafts: Word books being converted and checked in the workbench.

Shared by the workbench app (workbench/, where an employee uploads a .doc/.docx, compares
the converted pages with the original and fixes them) and the admin panel (where finished
drafts are reviewed and published). Both read the same folder, WORKBENCH_ROOT, which is a
plain directory -- one sub-folder per draft:

    <root>/drafts/<id>/draft.json     status, who/when, conversion options and report
                       source.doc[x]  the uploaded original
                       readme.txt     optional, the download's readme (title/author/...)
                       book.json      the current v2 book (converted, then edited)
                       original.pdf   the original rendered by LibreOffice, for comparison
                       img/0001.png   page images cut from original.pdf on demand

Deliberately no database table: a draft is not part of the catalog until it is published,
and a folder is trivially inspected, backed up or deleted by hand.

Status flow: editing -> submitted -> published, with "returned" (sent back by the
reviewer with a note) behaving like editing. Nothing here imports app.config, so the
workbench can use it without the API's database settings.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

EDITABLE = {"editing", "returned", "failed"}
STATUSES = EDITABLE | {"submitted", "published"}

_ID_RE = re.compile(r"^\d{8}-[0-9a-f]{6}$")


class DraftNotFound(Exception):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(3)}"


def draft_dir(root: Path, draft_id: str) -> Path:
    """The draft's folder -- only for a well-formed id, so a crafted id can never reach
    outside the drafts folder."""
    if not _ID_RE.match(draft_id or ""):
        raise DraftNotFound(draft_id)
    return root / "drafts" / draft_id


def _write_json(path: Path, data: dict) -> None:
    # Write-then-rename: a crash or a concurrent reader never sees a half-written file.
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def create(root: Path, *, source_name: str, source_bytes: bytes, created_by: str,
           readme_bytes: bytes | None = None) -> dict:
    draft_id = new_id()
    folder = draft_dir(root, draft_id)
    folder.mkdir(parents=True)
    ext = ".docx" if source_name.lower().endswith(".docx") else ".doc"
    (folder / f"source{ext}").write_bytes(source_bytes)
    if readme_bytes:
        (folder / "readme.txt").write_bytes(readme_bytes)
    meta = {
        "id": draft_id, "sourceName": source_name, "sourceFile": f"source{ext}",
        "status": "editing", "createdBy": created_by, "createdAt": now(),
        "updatedBy": created_by, "updatedAt": now(),
        "title": Path(source_name).stem, "author": "",
        "options": {}, "report": {}, "issues": [], "error": None,
        "render": {"status": "pending", "pages": 0, "error": None},
        "reviewNote": None, "publishedBookId": None,
    }
    _write_json(folder / "draft.json", meta)
    return meta


def load(root: Path, draft_id: str) -> dict:
    path = draft_dir(root, draft_id) / "draft.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DraftNotFound(draft_id) from None


def save(root: Path, meta: dict) -> dict:
    _write_json(draft_dir(root, meta["id"]) / "draft.json", meta)
    return meta


def update(root: Path, draft_id: str, **changes) -> dict:
    meta = load(root, draft_id)
    meta.update(changes)
    return save(root, meta)


def load_book(root: Path, draft_id: str) -> dict | None:
    path = draft_dir(root, draft_id) / "book.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_book(root: Path, draft_id: str, book: dict) -> None:
    _write_json(draft_dir(root, draft_id) / "book.json", book)


def list_all(root: Path) -> list[dict]:
    folder = root / "drafts"
    if not folder.exists():
        return []
    out = []
    for sub in folder.iterdir():
        if _ID_RE.match(sub.name) and (sub / "draft.json").exists():
            try:
                out.append(json.loads((sub / "draft.json").read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    return sorted(out, key=lambda m: m.get("updatedAt", ""), reverse=True)


def delete(root: Path, draft_id: str) -> None:
    folder = draft_dir(root, draft_id)
    if not folder.exists():
        raise DraftNotFound(draft_id)
    for path in sorted(folder.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    folder.rmdir()


def book_filename(title: str, fallback: str) -> str:
    """A download name made from the book's title, as the converter names its output."""
    safe = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", title or "").strip(" .")[:120]
    return f"{safe or fallback}.json"
