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


# Made by scripts/convert/pdf_pages.py from the book's printed PDF; uploaded beside the Word file.
PAGE_PLAN = "pages.json"


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

    plan_file = folder / PAGE_PLAN
    page_plan = json.loads(plan_file.read_text(encoding="utf-8")) if plan_file.exists() else None
    if page_plan:  # the print's pages: where the front matter ends is the plan's to say
        opts["frontPages"] = page_plan.get("frontPages", opts["frontPages"])
    content, _items = conv.convert_doc(
        folder / meta["sourceFile"], title, author or "", opts["frontPages"], opts["firstPrinted"],
        heading_re, "900001", use_toc=opts["useToc"], extra_metadata=extra,
        blank_pages=opts["blankPages"], page_plan=page_plan,
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
        "pageNumbersFrom": "printed-pdf" if page_plan else "toc" if anchors else "sequential",
        "anchors": len(anchors),
        "recovered": [{"page": n, "title": t, "how": how} for n, t, how in recovered],
    }
    report.update(source_report(conv, folder / meta["sourceFile"], content))
    if page_plan:
        report["pagesFrom"] = "printed-pdf"
    return content, report, issues_of(content) + report["sourceIssues"]


# Word's saved layout must cover the book: more than this share of its text laid out
# nowhere means Word never paginated it (it gets no pages of its own).
MAX_UNLAID_OUT = 0.05
# A book whose pages hold this many letters on average got no pages from its file: a real
# one with neither page breaks nor Word's saved layout came out as 24 "pages" for 332.
NO_PAGINATION_LETTERS = 6000
# Converted text may fall this far short of the file's before it is called a loss.
MIN_TEXT_COVERAGE = 0.995


def source_report(conv, source: Path, content: dict) -> dict:
    """What the Word file itself says about the conversion -- where the pages came from,
    whether any of its text was left out -- with issues for what needs the employee.
    Kept in the report, since saving an edited book re-checks the book, not the file."""
    issues: list[dict] = []
    out: dict = {"pagesFrom": "page-breaks"}
    if conv.is_docx(source):
        layout = conv.docx_layout(source)
        out["pagesFrom"] = layout["pagesFrom"]
        if layout["unlaidOut"] > MAX_UNLAID_OUT:
            issues.append({
                "severity": "error", "code": "word-layout-incomplete",
                "detail": f"صفحات هذا الملف مأخوذة من تخطيط Word المحفوظ فيه، لكن Word لم يُخطّط سوى "
                          f"{100 - 100 * layout['unlaidOut']:.0f}% منه عند آخر حفظ، فبقيته بلا صفحات. "
                          "الحل: افتحه في Word على جهاز مثبّتة عليه خطوط الكتاب نفسها (مثل Mosawi)، واذهب "
                          "إلى آخر صفحة (Ctrl+End) وانتظر حتى يكتمل عدد الصفحات، ثم احفظه وارفعه من جديد. "
                          "لا تحفظه على جهاز ينقصه خط الكتاب: سيحفظ Word صفحاتٍ غير صفحات الكتاب.",
            })
    pages = content.get("pages") or []
    letters = sum(len(b.get("text") or "") for pg in pages for b in pg.get("blocks") or [])
    if out["pagesFrom"] == "page-breaks" and pages and letters / len(pages) > NO_PAGINATION_LETTERS:
        issues.append({
            "severity": "error", "code": "no-page-information",
            "detail": f"هذا الملف لا يحمل معلومات عن صفحات الكتاب: لا فواصل صفحات فيه ولا تخطيط Word "
                      f"محفوظ، فخرج في {len(pages)} صفحة فقط. الحل: افتحه في Word على جهاز مثبّتة عليه "
                      "خطوط الكتاب، واذهب إلى آخر صفحة (Ctrl+End)، ثم احفظه بصيغة docx وارفعه من جديد.",
        })
    try:
        source_letters, converted = conv.text_coverage(source, content)
    except Exception:  # noqa: BLE001 -- the check must never stop a conversion
        source_letters, converted = 0, 0
    if source_letters:
        out["textCoverage"] = round(converted / source_letters, 4)
        if converted < source_letters * MIN_TEXT_COVERAGE:
            issues.append({
                "severity": "warning", "code": "text-loss",
                "detail": f"لم يُنقل حوالي {source_letters - converted} حرفاً من نص الملف "
                          f"({100 - 100 * converted / source_letters:.1f}%) — ربما في مربعات نص أو "
                          "عناصر لا يقرؤها المحوّل. قارن الكتاب بالأصل وأبلغ المسؤول.",
            })
    out["sourceIssues"] = issues
    return out


def issues_of(content: dict) -> list[dict]:
    val = converter().val
    found = [{"severity": i.severity, "code": i.code, "detail": i.detail} for i in val.validate(content)]
    return found + page_checks(content)


# A page this many times longer than the book's usual page probably holds two printed pages
# (a page break missing in the Word file); one this much shorter, in the middle of a
# chapter, probably half of one (a stray break). Pages come only from the file's breaks,
# so a typesetter's slip there is copied into the book -- these point the employee at it.
LONG_PAGE = 1.8
_CHAPTER_START_RE = re.compile(r"^\W*(ال)?(فصل|باب|مبحث|مجلس|قسم|مقصد|خاتمه|مقدمه|تمهيد)\b")
SHORT_PAGE = 0.3
MAX_PER_CHECK = 40
FOOTNOTE_WEIGHT = 0.6  # footnotes are set smaller: more of them fit on a page


def page_checks(content: dict) -> list[dict]:
    """Warnings, each with the page's position ("page", 0-based) so the editor can jump to it."""
    pages = content.get("pages") or []

    def length(p):  # footnotes count, at their smaller type: a page of mostly footnotes is full
        return sum(len(b.get("text") or "") * (FOOTNOTE_WEIGHT if b.get("type") == "footnotes" else 1)
                   for b in p.get("blocks") or [])

    def label(p, i):
        return f"{p.get('pageNumber')}" if str(p.get("pageNumber") or "").strip() else f"رقم {i + 1} في الترتيب"

    def starts_with_heading(p):  # a new chapter, marked as a heading or not
        blocks = [b for b in p.get("blocks") or [] if (b.get("text") or "").strip()]
        return bool(blocks) and (blocks[0].get("type") == "heading"
                                 or bool(_CHAPTER_START_RE.match(normalize(blocks[0]["text"]))))

    lengths = [length(p) for p in pages]
    main = sorted(n for p, n in zip(pages, lengths) if p.get("pageType") == "main" and n > 0)
    out: list[dict] = []
    if len(main) >= 10:
        usual = main[len(main) // 2]
        long_pages, short_pages = [], []
        for i, (p, n) in enumerate(zip(pages, lengths)):
            if p.get("pageType") != "main" or not n:
                continue
            # Against the pages around it: an index's pages are all dense, a page holding two
            # printed pages is twice its neighbours.
            around = sorted(m for m in lengths[max(0, i - 5):i] + lengths[i + 1:i + 6] if m)
            local = around[len(around) // 2] if around else usual
            if n > max(usual, local) * LONG_PAGE:
                long_pages.append({
                    "severity": "warning", "code": "long-page", "page": i,
                    "detail": f"الصفحة {label(p, i)} أطول من الصفحات حولها بـ{n / max(usual, local):.1f} مرة "
                              "— ربما ينقصها فاصل صفحة في ملف Word (استعمل ✂ لتقسيمها).",
                })
            elif (n < usual * SHORT_PAGE and 0 < i < len(pages) - 1
                  and lengths[i - 1] and lengths[i + 1]  # a title page stands next to a blank one
                  and not starts_with_heading(pages[i + 1]) and not starts_with_heading(p)
                  and any(b.get("type") == "text" for b in p.get("blocks") or [])):
                short_pages.append({
                    "severity": "warning", "code": "short-page", "page": i,
                    "detail": f"الصفحة {label(p, i)} قصيرة جداً وسط الفصل — ربما فاصل صفحة زائد "
                              "(ادمجها مع التالية إن كانت جزءاً منها).",
                })
        out += long_pages[:MAX_PER_CHECK] + short_pages[:MAX_PER_CHECK]

    jumps = []
    prev = None
    for i, p in enumerate(pages):
        num = str(p.get("pageNumber") or "").strip()
        if p.get("pageType") != "main" or not num.isdigit():
            continue
        n = int(num)
        if prev is not None and n != prev[1] + 1:
            back = n <= prev[1]
            jumps.append({
                "severity": "warning", "code": "number-back" if back else "number-skip", "page": i,
                "detail": (f"ترقيم الصفحات يرجع من {prev[1]} إلى {n}" if back
                           else f"ترقيم الصفحات يقفز من {prev[1]} إلى {n}")
                          + " — تحقّق من الصفحات هنا (صفحة ناقصة أو زائدة، أو رقم خاطئ).",
            })
        prev = (i, n)
    return out + jumps[:MAX_PER_CHECK]


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
        if not converter().is_docx(source):
            _soffice(["--convert-to", "docx", "--outdir", str(tmp_dir), str(source)], profile, timeout)
            source = tmp_dir / (source.stem + ".docx")
            if not source.exists():
                raise RuntimeError("LibreOffice could not read the .doc file")
        preview = tmp_dir / "preview.docx"
        if converter().docx_layout(source)["pagesFrom"] == "page-breaks":
            stretch_pages(source, preview)
        else:
            # Text that flows has no breaks of the book's own to keep a taller page in step
            # with: Word laid it out, and a page of the book's own size comes closest.
            shutil.copy(source, preview)
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


def use_pdf(folder: Path, data: bytes) -> int:
    """Use an uploaded PDF as the original to compare against -- one saved from Word
    itself matches Word exactly (its real fonts and page breaks), which a LibreOffice
    render of the Word file can't; a scan of the printed book works too. Returns its
    page count, or raises ValueError if it isn't a readable PDF."""
    if not data.startswith(b"%PDF"):
        raise ValueError("الملف ليس PDF")
    candidate = folder / ".uploaded.pdf"
    candidate.write_bytes(data)
    try:
        info = subprocess.run(["pdfinfo", str(candidate)], capture_output=True, text=True, timeout=60)
        match = re.search(r"^Pages:\s+(\d+)", info.stdout, re.MULTILINE)
        if info.returncode != 0 or not match or int(match.group(1)) < 1:
            raise ValueError("تعذّرت قراءة ملف PDF")
    except BaseException:
        candidate.unlink(missing_ok=True)
        raise
    candidate.replace(folder / "original.pdf")
    for stale in (folder / "img").glob("*.png") if (folder / "img").exists() else []:
        stale.unlink()
    return int(match.group(1))


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


def _locate_by_letters(book: dict, pdf_words: list[list[str]]) -> list[list[int] | None]:
    """Proposer 2 for page_map: [first, last, main] per converted page, found by runs of
    letters rather than words: spaces are dropped on both sides and runs of letters
    are looked up. PDF text extraction splits justified Arabic into fragments ("وس ار
    قاص دا" for "وسار قاصدا") and drops ligature glyphs, which breaks whole-word matching
    but leaves long letter runs intact."""
    page_of: list[int] = []
    chunks: list[str] = []
    for n, words in enumerate(pdf_words, 1):
        letters = "".join(words)
        chunks.append(letters)
        page_of.extend([n] * len(letters))
    stream = "".join(chunks)
    pages = book.get("pages", [])
    texts = ["".join(text_words(" ".join(b.get("text", "") for b in p.get("blocks", [])))) for p in pages]
    if not stream or len(stream) < sum(map(len, texts)) // 5:
        # Barely any text in the original: a scanned PDF. Nothing to match on -- the
        # viewer pairs pages by number (with an adjustable offset) instead.
        return []

    def probe_len(text: str) -> int:
        return 14 if len(text) >= 40 else max(6, len(text) // 2)

    def find(text: str, lo: int, hi: int, from_end: bool) -> int | None:
        """Stream position of the page's first (or last) letter, from a run of letters
        near its start (or end) found within [lo, hi]. Several runs are tried, in case one
        is damaged in the extraction."""
        k = probe_len(text)
        if len(text) < k:
            return None
        lo, hi = max(lo, 0), min(hi, len(stream))
        for step in range(0, min(len(text) - k + 1, 12 * k), max(k // 2, 3)):
            if from_end:
                probe = text[len(text) - k - step:len(text) - step]
                pos = stream.rfind(probe, lo, hi)
                if pos != -1:
                    return min(pos + k + step - 1, len(stream) - 1)
            else:
                probe = text[step:step + k]
                pos = stream.find(probe, lo, hi)
                if pos != -1:
                    return max(pos - step, 0)
        return None

    spans: list[tuple[int, int] | None] = []  # stream positions of each page's first/last letter
    cursor = 0
    skipped = 0  # letters of pages not located since the last one that was
    for text in texts:
        # Look ahead only as far as the pages skipped since the last match could reach.
        start = find(text, cursor, cursor + 3000 + int(skipped * 1.5), from_end=False)
        if start is None:
            spans.append(None)
            skipped += len(text)
            continue
        expected = start + len(text) - 1
        slack = max(120, len(text) // 4)
        # An ending found far from where the page's own length puts it belongs to another
        # page (the real ending was damaged in extraction): ignore it and estimate.
        end = find(text, max(start, expected - slack), expected + slack + 1, from_end=True)
        if end is None:
            end = min(expected, len(stream) - 1)
            cursor = start + (len(text) * 4) // 5  # an estimate: leave the next page findable
        else:
            cursor = end + 1
        spans.append((start, end))
        skipped = 0

    # A page ends before the next located page begins.
    located = [i for i, span in enumerate(spans) if span]
    for i, j in zip(located, located[1:]):
        start, end = spans[i]
        spans[i] = (start, max(start, min(end, spans[j][0] - 1)))

    def main_page(span: tuple[int, int]) -> int:
        """The rendered page holding most of the page's letters: where its breaks differ
        slightly from the original's, a few lines sit on a neighbouring page -- the page
        shown is the one that is really this page."""
        counts: dict[int, int] = {}
        for pos in range(span[0], span[1] + 1):
            counts[page_of[pos]] = counts.get(page_of[pos], 0) + 1
        return max(counts, key=lambda n: (counts[n], -n))

    result: list[list[int] | None] = [
        [page_of[span[0]], page_of[span[1]], main_page(span)] if span else None for span in spans
    ]

    # Runs of pages not located (no text of their own, or text that didn't match) take,
    # in order, the rendered pages between their located neighbours.
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
                result[i + r] = [a, max(a, b), a] if a <= last else [last, last, last]
        i = j
    # A rendered page between two consecutive pages' ranges (an overflow whose text didn't
    # extract cleanly, or a blank page) is counted with the page before it.
    for k in range(len(result) - 1):
        if result[k] and result[k + 1] and result[k + 1][0] - result[k][1] > 1:
            result[k] = [result[k][0], result[k + 1][0] - 1, result[k][2]]
    return result


def _locate_by_words(book: dict, pdf_words: list[list[str]], gram: int = 3) -> list[list[int] | None]:
    """Proposer 1 for page_map: [first, last] rendered pages per converted page, found by
    whole-word n-grams. Good on clean text; lost where extraction fragments words."""
    stream: list[str] = []
    page_of: list[int] = []
    for n, words in enumerate(pdf_words, 1):
        stream.extend(words)
        page_of.extend([n] * len(words))
    book_words = sum(len(text_words(" ".join(b.get("text", "") for b in p.get("blocks", []))))
                     for p in book.get("pages", []))
    if not stream or len(stream) < book_words // 5:
        # Barely any text in the original: a scanned PDF. Nothing to match on -- the
        # viewer pairs pages by number (with an adjustable offset) instead.
        return []
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


def _letters(text: str) -> str:
    return "".join(text_words(text))


def _overlap(page_letters: str, original_letters: str, probe: int = 12, every: int = 24) -> float:
    """Share of the page's letter runs that occur on an original page."""
    runs = [page_letters[i:i + probe] for i in range(0, max(len(page_letters) - probe, 1), every)]
    return sum(1 for r in runs if r in original_letters) / max(len(runs), 1)


def page_map(book: dict, pdf_words: list[list[str]]) -> list[list[int] | None]:
    """For each converted page (in order), [first, last, shown]: the rendered/original pages
    (1-based) its text spans, and the single page to show next to it -- or None.

    Two locators propose where each page is (whole words, and letter runs -- see each);
    neither is right everywhere, since PDF text extraction damages text in different ways.
    The page shown is then decided by the text itself: of the proposed pages and their
    neighbours, the one containing the most of this page's text. Returns [] for an
    original with barely any text (a scanned PDF), which the viewer pairs by number."""
    by_letters = _locate_by_letters(book, pdf_words)
    if not by_letters:
        return []
    by_words = _locate_by_words(book, pdf_words)
    originals = ["".join(words) for words in pdf_words]
    total = len(originals)
    result: list[list[int] | None] = []
    for i, page in enumerate(book.get("pages", [])):
        a = by_letters[i] if i < len(by_letters) else None
        b = by_words[i] if i < len(by_words) else None
        proposals = [x for x in (a and a[2], b and b[0], b and b[1]) if x]
        if not proposals:
            result.append(None)
            continue
        letters = _letters(" ".join(bl.get("text", "") for bl in page.get("blocks", [])))
        shown = proposals[0]
        if len(letters) >= 40:
            candidates = sorted({n + d for n in proposals for d in (-1, 0, 1) if 1 <= n + d <= total})
            scores = {n: _overlap(letters, originals[n - 1]) for n in candidates}
            best = max(candidates, key=lambda n: (scores[n], -abs(n - proposals[0])))
            if scores[best] > scores.get(shown, 0):
                shown = best
        first = min(x for x in (a and a[0], b and b[0], shown) if x)
        last = max(x for x in (a and a[1], b and b[1], shown) if x)
        result.append([first, last, shown])
    return _keep_in_step(book, originals, result)


# A match is trusted to anchor its neighbours when it holds this much of the page's text.
SURE_MATCH = 0.5
IN_STEP_WINDOW = 25  # pages either side whose trusted matches say where a page should be
IN_STEP_SLACK = 2    # PDF pages either side of where it should be that are compared


def _keep_in_step(book: dict, originals: list[str], matched: list[list[int] | None]) -> list[list[int] | None]:
    """Book pages and PDF pages run in the same order, but each page above was matched on
    its own: in a book whose text repeats (genealogies, poems, index lists) the locators
    propose pages far away, and only pages near those were compared -- in one real book
    280 of 603 pages were shown the wrong PDF page (page 147 with page 166, 38% alike,
    while page 147 held 88%). Here each page is compared again with the PDF pages where
    its trusted neighbours put it, and where the whole book's trusted matches put it (the
    same number, for a PDF of the book itself); a page too short to judge by its text
    (a title, a blank page) takes that page too."""
    import statistics

    total = len(originals)
    pages = book.get("pages", [])
    letters = [_letters(" ".join(bl.get("text", "") for bl in p.get("blocks", []))) for p in pages]

    def score(i: int, n: int) -> float:
        return _overlap(letters[i], originals[n - 1]) if len(letters[i]) >= 40 else 0.0

    trusted = {i: m[2] - (i + 1) for i, m in enumerate(matched) if m and score(i, m[2]) >= SURE_MATCH}
    if not trusted:
        return matched
    overall = int(statistics.median(trusted.values()))
    clamp = lambda n: min(max(n, 1), total)  # noqa: E731
    result: list[list[int] | None] = []
    for i, m in enumerate(matched):
        near = [o for j, o in trusted.items() if abs(j - i) <= IN_STEP_WINDOW]
        expected = clamp(i + 1 + (int(statistics.median(near)) if near else overall))
        whole = clamp(i + 1 + overall)
        candidates = {n for e in (expected, whole)
                      for n in range(e - IN_STEP_SLACK, e + IN_STEP_SLACK + 1) if 1 <= n <= total}
        if m:
            candidates |= {n + d for n in m for d in (-1, 0, 1) if 1 <= n + d <= total}
        if len(letters[i]) >= 40:
            scores = {n: score(i, n) for n in candidates}
            shown = max(candidates, key=lambda n: (scores[n], -abs(n - expected)))
            if scores[shown] == 0:
                shown = expected
        else:
            shown = expected
        if m and m[0] <= shown <= m[1] and m[1] - m[0] <= 1:
            result.append([m[0], m[1], shown])  # the text spills onto the next page or from the last
        else:
            result.append([shown, shown, shown])
    return result
