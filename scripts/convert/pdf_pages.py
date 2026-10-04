"""Make a page plan for a .doc book from its printed PDF, so the converted book has the
print's own pages.

    uv run --with pymupdf --with pyobjc-framework-Vision --with olefile \\
        python scripts/convert/pdf_pages.py book.doc printed.pdf -o pages.json

(on the server, which has no macOS OCR, the workbench makes the plan itself with Tesseract)

Word's layout rarely matches a typesetter's PDF (another font, other margins), and a .doc
does not even record Word's. The PDF does: it is the book as printed. Each PDF page's text
is read (its text layer, or macOS's Arabic OCR for a scan), matched word by word against
the book's text, and the plan says where each printed page begins in the book. Upload
pages.json beside the .doc; the converter then cuts the pages there, and takes the page
numbers from the page headers.

The page's header (the running title and number) and its footnotes are left out of the
matching: the footnotes are placed by the .doc itself, and only the body decides where a
page begins. OCR misreads a symbol-font honorific, so a page starting with one is marked
uncertain, and so is any page whose first words could not be matched; the summary lists
them for the employee to check against the PDF.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import difflib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import doc_to_json_v2 as conv  # noqa: E402

HEADER_BELOW = 0.12  # share of the page height: the running header, if any, is above it
FOOTNOTES_BELOW = 0.62  # a footnote can start only below this
ROW_GAP = 0.012  # lines this close in height are one row (the two halves of a verse)
NGRAM = 4
WORDS_PER_START = 6
MIN_TEXT_PAGE_WORDS = 100  # words of body text that make a page part of the text, not front matter

_FOOTNOTE_START = re.compile(r"^\W{0,2}[\d٠-٩]+\s*[\)\-–ـ]|^\(\s*[\d٠-٩]")
_NUMBERED_ENDS = re.compile(r"^\W*[\d٠-٩]{1,4}(?!\d)|(?<!\d)[\d٠-٩]{1,4}\W*$")
_NUMBER = re.compile(r"[\d٠-٩]+")
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "0123456789" * 2)


class PlanError(Exception):
    pass


# ── reading the PDF ─────────────────────────────────────────────────────────────

def _ocr(png: bytes) -> list[tuple[float, float, float, str]]:
    """Lines of a page image: macOS's Arabic OCR where there is one (the better reader),
    otherwise Tesseract (what the server has)."""
    try:
        import Vision  # noqa: F401
    except ImportError:
        return _ocr_tesseract(png)
    return _ocr_vision(png)


def _ocr_vision(png: bytes) -> list[tuple[float, float, float, str]]:
    import Vision
    from Foundation import NSData

    data = NSData.dataWithBytes_length_(png, len(png))
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setRecognitionLanguages_(["ar-SA"])
    request.setUsesLanguageCorrection_(False)
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        raise PlanError(f"OCR failed: {error}")
    lines = []
    for o in request.results():
        box = o.boundingBox()
        lines.append((1 - (box.origin.y + box.size.height), box.origin.x, box.size.width,
                      o.topCandidates_(1)[0].string()))
    return sorted(lines)


def _ocr_tesseract(png: bytes) -> list[tuple[float, float, float, str]]:
    """The same lines from Tesseract (apt: tesseract-ocr, tesseract-ocr-ara): its words,
    grouped by the line it found them on, each line's words read right to left."""
    import csv
    import io
    import shutil
    import subprocess
    import tempfile

    if shutil.which("tesseract") is None:
        raise PlanError("this PDF has no text layer, and no OCR is installed here "
                        "(macOS, or tesseract-ocr with tesseract-ocr-ara)")
    with tempfile.NamedTemporaryFile(suffix=".png") as image:
        image.write(png)
        image.flush()
        done = subprocess.run(["tesseract", image.name, "-", "-l", "ara", "--psm", "4", "tsv"],
                              capture_output=True, text=True, timeout=300)
    if done.returncode != 0:
        raise PlanError(f"OCR failed: {done.stderr.strip()[:200]}")
    rows = list(csv.DictReader(io.StringIO(done.stdout), delimiter="\t", quoting=csv.QUOTE_NONE))
    page = next((r for r in rows if r["level"] == "1"), None)
    if page is None:
        return []
    width, height = int(page["width"]), int(page["height"])
    by_line: dict[tuple, list] = {}
    for r in rows:
        if r["level"] == "5" and r["text"].strip():
            by_line.setdefault((r["block_num"], r["par_num"], r["line_num"]), []).append(r)
    lines = []
    for words in by_line.values():
        words.sort(key=lambda r: -int(r["left"]))
        left = min(int(r["left"]) for r in words)
        right = max(int(r["left"]) + int(r["width"]) for r in words)
        top = min(int(r["top"]) for r in words)
        lines.append((top / height, left / width, (right - left) / width,
                      " ".join(r["text"] for r in words)))
    return sorted(lines)


def pdf_lines(pdf: Path, progress=None) -> list[list[tuple[float, float, float, str]]]:
    """Per page: (top, left, width, text) for each line of text, top and left as shares of
    the page."""
    try:
        import pymupdf
    except ImportError as exc:
        raise PlanError("pymupdf is required: uv run --with pymupdf ...") from exc
    doc = pymupdf.open(str(pdf))
    pages = []
    for i, page in enumerate(doc):
        w, h = page.rect.width, page.rect.height
        lines = []
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                text = "".join(span["text"] for span in line["spans"]).strip()
                if text:
                    x0, y0, x1, _ = line["bbox"]
                    lines.append((y0 / h, x0 / w, (x1 - x0) / w, text))
        if sum(len(t) for *_, t in lines) < 20:  # a scan: no text layer on this page
            lines = _ocr(page.get_pixmap(dpi=200).tobytes("png"))
        pages.append(sorted(lines))
        if progress:
            progress(i + 1, len(doc))
    return pages


def split_page(lines) -> tuple[str, list[str]]:
    """(the running header, the body's rows of text, right to left)."""
    rows: list[list] = []
    for top, left, _, text in lines:
        if rows and abs(rows[-1][0] - top) < ROW_GAP:
            rows[-1][1].append((left, text))
        else:
            rows.append([top, [(left, text)]])
    header, body, in_footnotes = "", [], False
    for k, (top, cells) in enumerate(rows):
        text = " ".join(t for _, t in sorted(cells, key=lambda c: -c[0]))
        # The running header is the first row, and has the page number at one end of it; a
        # chapter's first page has none, and its first row is text.
        if k == 0 and top < HEADER_BELOW and _NUMBERED_ENDS.search(text.strip()):
            header = text
            continue
        if top > FOOTNOTES_BELOW and _FOOTNOTE_START.match(text.strip()):
            in_footnotes = True
        if not in_footnotes:
            body.append(text)
    return header.strip(), body


def printed_number(header: str) -> int | None:
    """The page number in the running header: its first or last number, 1-4 digits."""
    found = [m.group(0).translate(_ARABIC_DIGITS) for m in _NUMBER.finditer(header)]
    found = [f for f in found if len(f) <= 4]
    return int(found[0]) if found else None


# ── the printed contents list ───────────────────────────────────────────────────

_LEADERS = re.compile(r"[.…·]{6,}")
_ONLY_NUMBER = re.compile(r"^\W*(\d{1,5})\W*$")
CONTENTS_MIN_ROWS = 5
CONTENTS_SHARE = 0.7  # of a page's rows must be a title with its page number


def _contents_rows(lines, page_count: int) -> list[tuple[str, int]]:
    """(title, page) for each row of a page of the contents list: a title cell at the right
    and a number cell at the left on the same row. Dot leaders read as zeros after a number
    ("١٦٨٠٠" for 168), so a number past the book's last page loses its trailing 00."""
    rows: list[list] = []
    for top, left, _, text in sorted(lines):
        if rows and abs(rows[-1][0] - top) < ROW_GAP / 2:
            rows[-1][1].append((left, text))
        else:
            rows.append([top, [(left, text)]])
    found = []
    for _, cells in rows:
        cells.sort()
        number, title = None, []
        for left, text in cells:
            text = text.translate(_ARABIC_DIGITS)
            m = _ONLY_NUMBER.match(text)
            if m and number is None and left < 0.4:
                number = int(m.group(1))
            else:
                title.append((left, text))
        if number is None or not title:
            continue
        while number > page_count and number % 100 == 0 or number > 10 * page_count:
            number //= 100 if number % 100 == 0 else 10
        text = " ".join(t for _, t in sorted(title, key=lambda c: -c[0]))
        text = re.sub(r"[.…·]{2,}|^[\s.:\-–]+|[\s.]+$", " ", text).strip()
        text = re.sub(r"[\s.:]+[0٠]{1,2}$", "", text)  # a dot leader read as a zero
        if len(text) > 2 and 0 < number <= page_count:
            found.append((text, number))
    return found


def contents_entries(pdf: Path, lines_by_page, page_count: int, progress=None) -> list[dict]:
    """The book's own contents list, read from the PDF's pages that are one: most of their
    rows are a title with a page number. Read by OCR, which keeps each row's two cells
    together where a text layer mixes their order."""
    candidates = [k for k, lines in enumerate(lines_by_page)
                  if sum(1 for *_, t in lines if _LEADERS.search(t)) >= CONTENTS_MIN_ROWS
                  or sum(1 for *_, t in lines if _ONLY_NUMBER.match(t.translate(_ARABIC_DIGITS))) >= CONTENTS_MIN_ROWS]
    if not candidates:
        return []
    import pymupdf
    doc = pymupdf.open(str(pdf))
    entries: list[dict] = []
    for k in candidates:
        try:
            ocr_lines = _ocr(doc[k].get_pixmap(dpi=200).tobytes("png"))
        except PlanError:
            ocr_lines = lines_by_page[k]
        rows = _contents_rows(ocr_lines, page_count)
        body_rows = sum(1 for *_, t in ocr_lines if t.strip())
        if len(rows) >= CONTENTS_MIN_ROWS and len(rows) >= CONTENTS_SHARE * (body_rows / 2):
            entries += [{"title": t, "page": n, "pdfPage": k + 1} for t, n in rows]
    # The list is in page order: a number far past the next ones, ending in 0, is a number
    # with a dot leader read as a zero ("90" for 9).
    for e, following in zip(entries, entries[1:]):
        if e["page"] % 10 == 0 and e["page"] > following["page"] + 20:
            e["page"] //= 10
    return entries


# ── matching ────────────────────────────────────────────────────────────────────

def _chain(ocr: list[str], text: list[str]) -> list[tuple[int, int]]:
    """Runs of NGRAM words that occur once in each and agree on order: the book's fixed
    points in the PDF."""
    def grams(seq):
        d = collections.defaultdict(list)
        for i in range(len(seq) - NGRAM + 1):
            d[tuple(seq[i:i + NGRAM])].append(i)
        return d
    g_text, g_ocr = grams(text), grams(ocr)
    anchors = sorted((g_ocr[g][0], g_text[g][0]) for g in g_ocr
                     if len(g_ocr[g]) == 1 and len(g_text.get(g, ())) == 1)
    tails: list[int] = []
    tail_at: list[int] = []
    prev = [-1] * len(anchors)
    for k, (_, w) in enumerate(anchors):
        j = bisect.bisect_left(tails, w)
        if j == len(tails):
            tails.append(w)
            tail_at.append(k)
        else:
            tails[j] = w
            tail_at[j] = k
        prev[k] = tail_at[j - 1] if j else -1
    chain, k = [], tail_at[-1] if tail_at else -1
    while k != -1:
        chain.append(anchors[k])
        k = prev[k]
    return chain[::-1]


def _locate(o: int, chain, ocr, text) -> tuple[int, bool]:
    """Word of the book that the PDF's word o is, and whether it matched exactly."""
    starts = [a for a, _ in chain]
    j = bisect.bisect_right(starts, o)
    o0, w0 = chain[j - 1] if j else (0, 0)
    o1, w1 = chain[j] if j < len(chain) else (len(ocr), len(text))
    o1, w1 = min(len(ocr), o1 + NGRAM), min(len(text), w1 + NGRAM)
    matcher = difflib.SequenceMatcher(None, ocr[o0:o1], text[w0:w1], autojunk=False)
    rel = o - o0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if i1 <= rel < i2 or (rel == i1 and tag == "insert"):
            return (w0 + j1 + (rel - i1), True) if tag == "equal" else (w0 + j1, False)
    return w0 + min(rel, w1 - w0), False


def make_plan(doc: Path, pdf: Path, progress=None) -> dict:
    paras = conv.read_any(doc, body_only=True)
    units = conv.body_units(paras)
    text = [u[3] for u in units]
    pages = pdf_lines(pdf, progress)

    ocr: list[str] = []
    first_word: list[int | None] = []
    numbers: list[int | None] = []
    for lines in pages:
        header, body = split_page(lines)
        numbers.append(printed_number(header))
        words = [t for row in body for t in (conv.unit_token(c) for c in row.split()) if t]
        first_word.append(len(ocr) if words else None)
        ocr += words
    chain = _chain(ocr, text)
    if len(chain) < 0.3 * min(len(ocr), len(text)) / NGRAM:
        raise PlanError("the PDF's text hardly matches the book's: is it the same book? "
                        f"({len(chain)} fixed points in {len(ocr)} PDF words and {len(text)} book words)")

    located: list[tuple[int, bool] | None] = [
        _locate(o, chain, ocr, text) if o is not None and o < len(ocr) else None for o in first_word]
    # A page with no text of its own (a picture, a blank) begins where the next one does.
    after = None
    for k in range(len(located) - 1, -1, -1):
        if located[k] is None:
            located[k] = (after[0], False) if after else (len(text), False)
        else:
            after = located[k]
    pos, sure = [], []
    for k, (w, exact) in enumerate(located):
        if pos and w < pos[-1]:  # a mismatch must not send the text backwards
            w, exact = pos[-1], False
        pos.append(w)
        sure.append(exact and first_word[k] is not None)

    # Printed numbers: the header's number where it agrees with the usual offset from the
    # PDF's page index, else the next after the page before; pages before the first
    # numbered one are front matter.
    offsets = collections.Counter(n - k for k, n in enumerate(numbers) if n is not None)
    offset = offsets.most_common(1)[0][0] if offsets else 0
    front = next((k for k, n in enumerate(numbers) if n is not None and n - k == offset), 0)
    # A page whose number was not read (or never printed) but holds a page of text before
    # the first numbered one is the text, not a cover or a title page.
    sizes = []
    for k, first in enumerate(first_word):
        later = next((f for f in first_word[k + 1:] if f is not None), len(ocr))
        sizes.append(0 if first is None else later - first)
    while front > 0 and sizes[front - 1] >= MIN_TEXT_PAGE_WORDS:
        front -= 1

    plan_pages = []
    for k, w in enumerate(pos):
        unit = units[min(w, len(units) - 1)]
        plan_pages.append({
            "pdfPage": k + 1, "para": unit[0], "chunk": unit[1],
            "words": " ".join(text[w:w + WORDS_PER_START]),
            "label": str(k + offset) if k >= front else None,
            "sure": sure[k],
        })
    plan = {"version": 1, "source": doc.name, "pdf": pdf.name, "frontPages": front,
            "pages": plan_pages}
    toc = contents_entries(pdf, pages, len(pages))
    if toc:
        plan["toc"] = toc
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("doc", type=Path)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("-o", "--output", type=Path, default=Path("pages.json"))
    args = parser.parse_args()

    def progress(done, total):
        if done % 40 == 0 or done == total:
            print(f"read {done}/{total} PDF pages", file=sys.stderr, flush=True)
    try:
        plan = make_plan(args.doc, args.pdf, progress)
    except (PlanError, conv.DocError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    args.output.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    pages = plan["pages"]
    unsure = [p["pdfPage"] for p in pages if not p["sure"]]
    print(f"{len(pages)} printed pages -> {args.output}; front matter {plan['frontPages']} pages; "
          f"{len(pages) - len(unsure)} matched exactly")
    print(f"contents list read from the PDF: {len(plan.get('toc', []))} entries")
    if unsure:
        print("check these PDF pages against the converted book:", ", ".join(map(str, unsure)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
