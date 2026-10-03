"""Make a page plan for a .doc book from its printed PDF, so the converted book has the
print's own pages.

    uv run --with pymupdf --with pyobjc-framework-Vision --with olefile \\
        python scripts/convert/pdf_pages.py book.doc printed.pdf -o pages.json

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

_FOOTNOTE_START = re.compile(r"^\W{0,2}[\d٠-٩]+\s*[\)\-–ـ]|^\(\s*[\d٠-٩]")
_NUMBERED_ENDS = re.compile(r"^\W*[\d٠-٩]{1,4}(?!\d)|(?<!\d)[\d٠-٩]{1,4}\W*$")
_NUMBER = re.compile(r"[\d٠-٩]+")
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "0123456789" * 2)


class PlanError(Exception):
    pass


# ── reading the PDF ─────────────────────────────────────────────────────────────

def _ocr(png: bytes) -> list[tuple[float, float, float, str]]:
    try:
        import Vision
        from Foundation import NSData
    except ImportError as exc:
        raise PlanError("this PDF has no text layer and OCR needs macOS: "
                        "uv run --with pyobjc-framework-Vision ...") from exc
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
    paras = conv.read_doc(doc, body_only=True)
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

    plan_pages = []
    for k, w in enumerate(pos):
        unit = units[min(w, len(units) - 1)]
        plan_pages.append({
            "pdfPage": k + 1, "para": unit[0], "chunk": unit[1],
            "words": " ".join(text[w:w + WORDS_PER_START]),
            "label": str(k + offset) if k >= front else None,
            "sure": sure[k],
        })
    return {"version": 1, "source": doc.name, "pdf": pdf.name, "frontPages": front,
            "pages": plan_pages}


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
    if unsure:
        print("check these PDF pages against the converted book:", ", ".join(map(str, unsure)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
