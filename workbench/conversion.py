"""Run the Word converter on a draft, and render the original for side-by-side checking.

The conversion itself is scripts/convert/doc_to_json_v2.py's `convert_doc`, unchanged --
the workbench only supplies its options and keeps its report. The original is rendered
by LibreOffice to a PDF once per draft, and single pages are cut from that PDF as images
on demand by pdftoppm (poppler), cached next to it.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unicodedata
from pathlib import Path

from app.services.arabic import normalize

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


# How much taller than the book's own pages the preview is rendered. The books' Word
# fonts can't be reproduced exactly, and every free substitute sets Arabic with taller
# lines, so a full Word page doesn't fit on a rendered page of the same size and spills
# onto a second one. With taller pages each Word page's text stays on one rendered page,
# with the same page breaks -- the whole page can be seen at once. (A real 156-page book
# rendered as 278 pages unstretched, and as exactly 156 from 1.3 up.)
PREVIEW_PAGE_STRETCH = 1.35

_PGSZ_HEIGHT_RE = re.compile(r'(<w:pgSz\b[^>]*?\bw:h=")(\d+)(")')


def stretch_pages(src: Path, dst: Path, factor: float = PREVIEW_PAGE_STRETCH) -> None:
    """Copy a .docx with every section's page height multiplied by `factor` -- only for
    rendering the preview; the uploaded original is never changed."""
    import zipfile

    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                xml = data.decode("utf-8")
                xml = _PGSZ_HEIGHT_RE.sub(lambda m: f"{m.group(1)}{round(int(m.group(2)) * factor)}{m.group(3)}", xml)
                data = xml.encode("utf-8")
            zout.writestr(item, data)


def _soffice(args: list[str], profile: Path, timeout: int) -> None:
    subprocess.run(["soffice", f"-env:UserInstallation=file://{profile}", "--headless", "--norestore", *args],
                   check=True, timeout=timeout, capture_output=True)


def render_pdf(folder: Path, source_file: str, timeout: int = 600) -> int:
    """Render the source to original.pdf (pages stretched, see PREVIEW_PAGE_STRETCH);
    returns its page count."""
    with _render_lock, tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        profile = tmp_dir / "profile"
        source = folder / source_file
        if source.suffix.lower() == ".doc":
            _soffice(["--convert-to", "docx", "--outdir", str(tmp_dir), str(source)], profile, timeout)
            source = tmp_dir / (source.stem + ".docx")
            if not source.exists():
                raise RuntimeError("LibreOffice could not read the .doc file")
        preview = tmp_dir / "preview.docx"
        stretch_pages(source, preview)
        _soffice(["--convert-to", "pdf", "--outdir", str(tmp_dir), str(preview)], profile, timeout)
        produced = tmp_dir / "preview.pdf"
        if not produced.exists():
            raise RuntimeError("LibreOffice produced no PDF")
        shutil.move(str(produced), folder / "original.pdf")
    for stale in (folder / "img").glob("*.png") if (folder / "img").exists() else []:
        stale.unlink()
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


# ── lining converted pages up with the rendered original ─────────────────────
#
# LibreOffice does not break pages where Word does: these books are set in Traditional
# Arabic, whose tight line height no free font reproduces, so every Word page overflows
# into a short extra page (a 156-page book rendered as 278). Page N of the render is
# therefore not page N of the book. Instead each converted page is located in the render
# by its own words -- where its opening words and its closing words fall -- and shown as
# the range of rendered pages it spans.

_WORD_RE = re.compile(r"[\u0621-\u064A\u0660-\u0669\u06F0-\u06F90-9A-Za-z]{2,}")


def text_words(text: str) -> list[str]:
    """Words for matching: presentation forms unfolded (PDF text extraction yields them),
    then the search normalizer (tashkeel, hamza forms ...), one-letter tokens dropped."""
    return _WORD_RE.findall(normalize(unicodedata.normalize("NFKC", text or "")))


def extract_original_words(folder: Path, pages: int) -> None:
    """Save each rendered page's words (original_words.json), once per render."""
    out = []
    for n in range(1, pages + 1):
        text = subprocess.run(["pdftotext", "-f", str(n), "-l", str(n), str(folder / "original.pdf"), "-"],
                              capture_output=True, text=True, timeout=60).stdout
        out.append(text_words(text))
    (folder / "original_words.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")


def page_map(book: dict, pdf_words: list[list[str]], gram: int = 3) -> list[list[int] | None]:
    """For each converted page (in order), [first, last] rendered page it spans (1-based),
    or None where it can't be located."""
    stream: list[str] = []
    page_of: list[int] = []
    for n, words in enumerate(pdf_words, 1):
        stream.extend(words)
        page_of.extend([n] * len(words))
    if not stream:
        return [None] * len(book.get("pages", []))
    index: dict[tuple, list[int]] = {}
    for i in range(len(stream) - gram + 1):
        index.setdefault(tuple(stream[i:i + gram]), []).append(i)

    def find(words: list[str], lo: int, hi: int, from_end: bool) -> int | None:
        """Stream position of the page's first (or last) word, found via an n-gram near
        its start (or end) occurring within [lo, hi]. A few n-grams are tried in case one
        differs (a heading or a verse extracted in another order, a footnote marker)."""
        if len(words) < gram:
            return None
        if from_end:
            starts = range(len(words) - gram, max(len(words) - gram - 12, -1), -1)
        else:
            starts = range(0, min(12, len(words) - gram + 1))
        for k in starts:
            for pos in index.get(tuple(words[k:k + gram]), []):
                if pos > hi:
                    break
                if pos >= lo:
                    # Where the page's own first (last) word is, from where this n-gram sits
                    # in it -- not clamped to `lo`: an overlap with the page before is
                    # resolved below by trimming that page, which is the one that guessed.
                    at = pos + (len(words) - gram - k if from_end else -k)
                    return min(max(at, 0), len(stream) - 1)
        return None

    spans: list[tuple[int, int] | None] = []  # stream positions of each page's first/last word
    cursor = 0
    skipped = 0  # words of pages not located since the last one that was
    for page in book.get("pages", []):
        words = text_words(" ".join(b.get("text", "") for b in page.get("blocks", [])))
        # Look ahead only as far as the pages skipped since the last match could reach.
        start = find(words, cursor, cursor + 600 + int(skipped * 1.5), from_end=False)
        if start is None:
            spans.append(None)
            skipped += len(words)
            continue
        expected = start + len(words) - 1
        slack = max(25, len(words) // 3)
        # An ending found far from where the page's own length puts it is another page's
        # copy of the same phrase (the real ending was garbled in extraction): ignore it.
        end = find(words, max(start, expected - slack), expected + slack, from_end=True)
        if end is None:
            end = min(expected, len(stream) - 1)
            cursor = start + (len(words) * 4) // 5  # an estimate: leave the next page findable
        else:
            cursor = end + 1
        spans.append((start, end))
        skipped = 0

    # A page ends before the next located page begins.
    located = [i for i, span in enumerate(spans) if span]
    for i, j in zip(located, located[1:]):
        start, end = spans[i]
        spans[i] = (start, max(start, min(end, spans[j][0] - 1)))
    result: list[list[int] | None] = [
        [page_of[span[0]], page_of[span[1]]] if span else None for span in spans
    ]

    # Runs of pages not located (no text of their own, or text that didn't match) share
    # out, in order, the rendered pages lying between their located neighbours.
    i = 0
    while i < len(result):
        if result[i] is not None:
            i += 1
            continue
        j = i
        while j < len(result) and result[j] is None:
            j += 1
        first = result[i - 1][1] + 1 if i > 0 else 1
        last = result[j][0] - 1 if j < len(result) else len(pdf_words)
        free = last - first + 1
        run = j - i
        if free >= 1:
            for r in range(run):
                a = first + r * free // run
                b = first + (r + 1) * free // run - 1
                result[i + r] = [a, max(a, b)] if a <= last else [last, last]
        i = j
    # A rendered page between two consecutive pages' ranges (an overflow whose text didn't
    # extract cleanly, or a blank page) is shown with the page before it, so every page of
    # the original can be seen next to something.
    for k in range(len(result) - 1):
        if result[k] and result[k + 1] and result[k + 1][0] - result[k][1] > 1:
            result[k] = [result[k][0], result[k + 1][0] - 1]
    return result
