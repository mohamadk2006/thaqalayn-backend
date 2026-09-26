"""Run the Word converter on a draft, and render the original for side-by-side checking.

The conversion itself is scripts/convert/doc_to_json_v2.py's `convert_doc`, unchanged --
the workbench only supplies its options and keeps its report. The original is rendered
by LibreOffice to a PDF once per draft, and single pages are cut from that PDF as images
on demand by pdftoppm (poppler), cached next to it.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_converter = None
_converter_lock = threading.Lock()


def converter():
    """doc_to_json_v2 as a module (it is a standalone script, not a package)."""
    global _converter
    with _converter_lock:
        if _converter is None:
            path = ROOT / "scripts" / "convert" / "doc_to_json_v2.py"
            spec = importlib.util.spec_from_file_location("doc_to_json_v2", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules.setdefault("doc_to_json_v2", module)
            spec.loader.exec_module(module)
            _converter = module
    return _converter


# The converter's own metadata keys (see its CLI), which the workbench's metadata form edits.
_EXTRA_KEYS = ("publisher", "edition", "publicationYear", "authorDeath", "editor", "isbn",
               "language", "volume", "notes")


def default_options() -> dict:
    return {"blankPages": [], "frontPages": 0, "firstPrinted": 1, "useToc": True, "headingStyles": ""}


def clean_options(raw: dict | None) -> dict:
    opts = default_options()
    raw = raw or {}
    blanks = raw.get("blankPages", [])
    if isinstance(blanks, str):
        blanks = [b for b in re.split(r"[,\s،]+", blanks) if b]
    opts["blankPages"] = sorted({int(b) for b in blanks if str(b).strip().isdigit()})
    for key in ("frontPages", "firstPrinted"):
        value = raw.get(key, opts[key])
        opts[key] = int(value) if str(value).strip().lstrip("-").isdigit() else opts[key]
    opts["frontPages"] = max(opts["frontPages"], 0)
    opts["useToc"] = bool(raw.get("useToc", True))
    opts["headingStyles"] = str(raw.get("headingStyles") or "").strip()
    return opts


def convert(folder: Path, meta: dict, keep: dict | None = None) -> tuple[dict, dict, list[dict]]:
    """Convert the draft's source with meta["options"]. `keep` is the previous book (if
    any): its title, author and metadata survive a re-conversion, since those are the
    employee's own edits, not something the converter derives."""
    conv = converter()
    opts = clean_options(meta.get("options"))
    readme = conv.read_readme(folder / "readme.txt") if (folder / "readme.txt").exists() else {}
    keep = keep or {}
    kept_meta = keep.get("metadata") or {}

    title = keep.get("title") or readme.get("title") or meta.get("title") or "كتاب بلا عنوان"
    author = keep.get("author") if keep else readme.get("author", "")
    extra = {k: kept_meta.get(k) or readme.get(k, "") for k in _EXTRA_KEYS}

    if opts["headingStyles"]:
        wanted = {s.strip() for s in opts["headingStyles"].split(",") if s.strip()}
        heading_re = re.compile("^(" + "|".join(re.escape(s) for s in wanted) + ")$")
    else:
        heading_re = conv.HEADING_STYLE_RE

    content, _items = conv.convert_doc(
        folder / meta["sourceFile"], title, author or "", opts["frontPages"], opts["firstPrinted"],
        heading_re, "900001", use_toc=opts["useToc"], extra_metadata=extra,
        blank_pages=opts["blankPages"],
    )
    anchors = content.pop("_anchors")
    recovered = content.pop("_recovered")
    # Anything else the employee put in metadata (printer, trusted, ...) is kept too.
    for key, value in kept_meta.items():
        content["metadata"].setdefault(key, value)

    pages = content["pages"]
    main_pages = [p for p in pages if p["pageType"] == "main"]
    report = {
        "pages": len(pages),
        "frontPages": len(pages) - len(main_pages),
        "printedFirst": main_pages[0]["pageNumber"] if main_pages else None,
        "printedLast": main_pages[-1]["pageNumber"] if main_pages else None,
        "tocEntries": len(content["toc"]),
        "pageNumbersFrom": "toc" if anchors else "sequential",
        "anchors": len(anchors),
        "recovered": [{"page": n, "title": t, "how": how} for n, t, how in recovered],
    }
    return content, report, issues_of(content)


def issues_of(content: dict) -> list[dict]:
    val = converter().val
    return [{"severity": i.severity, "code": i.code, "detail": i.detail} for i in val.validate(content)]


# ── the original, rendered ───────────────────────────────────────────────────

_render_lock = threading.Lock()  # one LibreOffice at a time: it is memory-hungry


def renderer_available() -> bool:
    return bool(shutil.which("soffice") and shutil.which("pdftoppm") and shutil.which("pdfinfo"))


def render_pdf(folder: Path, source_file: str, timeout: int = 600) -> int:
    """Render the source to original.pdf; returns its page count."""
    with _render_lock, tempfile.TemporaryDirectory() as tmp:
        profile = Path(tmp) / "profile"
        subprocess.run(
            ["soffice", f"-env:UserInstallation=file://{profile}", "--headless", "--norestore",
             "--convert-to", "pdf", "--outdir", tmp, str(folder / source_file)],
            check=True, timeout=timeout, capture_output=True,
        )
        produced = Path(tmp) / (Path(source_file).stem + ".pdf")
        if not produced.exists():
            raise RuntimeError("LibreOffice produced no PDF")
        shutil.move(str(produced), folder / "original.pdf")
    info = subprocess.run(["pdfinfo", str(folder / "original.pdf")], check=True,
                          capture_output=True, text=True, timeout=60).stdout
    match = re.search(r"^Pages:\s+(\d+)", info, re.MULTILINE)
    return int(match.group(1)) if match else 0


def page_image(folder: Path, number: int) -> Path:
    """PNG of one original page (1-based), rendered once and cached."""
    out_dir = folder / "img"
    out_dir.mkdir(exist_ok=True)
    target = out_dir / f"{number:04d}.png"
    if not target.exists():
        stem = out_dir / f".{number:04d}"
        subprocess.run(
            ["pdftoppm", "-f", str(number), "-l", str(number), "-r", "110", "-png", "-singlefile",
             str(folder / "original.pdf"), str(stem)],
            check=True, timeout=60, capture_output=True,
        )
        stem.with_suffix(".png").replace(target)
    return target
