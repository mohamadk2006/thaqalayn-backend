#!/usr/bin/env python3
"""Convert a legacy Word 97-2003 .doc straight to the v2 book JSON -- no Word, no
LibreOffice, no intermediate .docx (converting through macOS textutil throws away the
paragraph styles this needs).

What it recovers from the binary that a plain text dump can't:
  * paragraph style names, so real chapter headings (style "Heading ...") become toc
    entries instead of ordinary text;
  * manual page breaks, which in books exported from sites like rafed.net fall exactly on
    the printed page boundaries;
  * the footnote separator rule (a line of underscores) -- everything after it on a page
    is emitted as `footnotes` blocks instead of body text.

Printed page numbers are not stored in a .doc (the running header only holds a PAGE
field that Word evaluates at layout time). They are recovered from the book's own table of
contents instead: each "TOC n" entry ends in a printed number, its title is located in the
body, and the offset between position and printed number is applied until the next anchor
(a mismatched anchor is dropped, and two Word pages that share one printed number are
merged). Those same matches become the toc[] headings. Without a usable TOC it falls back
to `--front-pages` / `--first-printed`. Always spot-check one real page.

The result is built by handing a synthetic Shamela-style text to the same convert() the
library's other books use, then validated with validate_book_v2, so structure is identical
by construction.

Usage:
    python scripts/convert/doc_to_json_v2.py book.doc --title "..." [--author "..."]
        [--out book.json] [--dump-styles] [--no-toc --front-pages N --first-printed N]

Also reads a .docx (paragraphs AND tables, in order -- a table row of several cells is a
verse, joined with " * " like the library's other poetry).

Needs `olefile` for .doc and `python-docx` for .docx:
    uv run --with olefile --with python-docx python scripts/convert/doc_to_json_v2.py book.doc \\
        --readme readme.txt
"""

from __future__ import annotations

import argparse
import bisect
import collections
import json
import re
import struct
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "convert"))
sys.path.insert(0, str(ROOT / "scripts" / "validate"))

import shamela_to_json_v2 as conv  # noqa: E402
import validate_book_v2 as val  # noqa: E402

SEPARATOR_RE = re.compile(r"^[_\-–—=]{5,}$")
HEADING_STYLE_RE = re.compile(r"^(heading\b|title\b)", re.IGNORECASE)


class DocError(Exception):
    pass


@dataclass
class Para:
    text: str
    style: str
    heading: bool = False


# ── .doc binary parsing ───────────────────────────────────────────────────────


def _pieces(wd: bytes, tbl: bytes):
    fc_clx, lcb_clx = struct.unpack_from("<II", wd, 0x01A2)
    clx = tbl[fc_clx:fc_clx + lcb_clx]
    i = 0
    while clx[i] == 1:
        i += 3 + struct.unpack_from("<H", clx, i + 1)[0]
    if clx[i] != 2:
        raise DocError("unsupported .doc: no piece table")
    n = struct.unpack_from("<I", clx, i + 1)[0]
    plc = clx[i + 5:i + 5 + n]
    count = (n - 4) // 12
    cps = struct.unpack_from("<%dI" % (count + 1), plc, 0)
    pieces = []  # (cp_start, cp_end, byte_offset, unit_size)
    base = 4 * (count + 1)
    for k in range(count):
        fc = struct.unpack_from("<I", plc, base + 8 * k + 2)[0]
        if fc & 0x40000000:
            pieces.append((cps[k], cps[k + 1], (fc & 0x3FFFFFFF) // 2, 1))
        else:
            pieces.append((cps[k], cps[k + 1], fc, 2))
    return pieces


def _story_text(wd: bytes, pieces, length: int) -> str:
    out = []
    for cp0, cp1, off, unit in pieces:
        if cp0 >= length:
            break
        cnt = min(cp1, length) - cp0
        if unit == 1:
            out.append(wd[off:off + cnt].decode("cp1252", "replace"))
        else:
            out.append(wd[off:off + cnt * 2].decode("utf-16le", "replace"))
    return "".join(out)


def _style_names(wd: bytes, tbl: bytes) -> list[str]:
    fc, lcb = struct.unpack_from("<II", wd, 0x00A2)
    if not lcb:
        return []
    data = tbl[fc:fc + lcb]
    cb_stshi = struct.unpack_from("<H", data, 0)[0]
    cstd, cb_base = struct.unpack_from("<HH", data, 2)
    pos = 2 + cb_stshi
    names = []
    for _ in range(cstd):
        cb_std = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        if cb_std == 0:
            names.append("")
            continue
        std = data[pos:pos + cb_std]
        cch = struct.unpack_from("<H", std, cb_base)[0]
        names.append(std[cb_base + 2:cb_base + 2 + cch * 2].decode("utf-16le", "replace"))
        pos += cb_std + (cb_std & 1)
    return names


def _paragraph_istds(wd: bytes, tbl: bytes):
    """Sorted (fc_start, fc_end, istd) runs from the PAPX bin table."""
    fc, lcb = struct.unpack_from("<II", wd, 0x0102)
    plc = tbl[fc:fc + lcb]
    n = (lcb - 4) // 8
    fcs = struct.unpack_from("<%dI" % (n + 1), plc, 0)
    pns = struct.unpack_from("<%dI" % n, plc, 4 * (n + 1))
    runs = []
    for pn in pns:
        page = wd[pn * 512:(pn + 1) * 512]
        crun = page[511]
        rgfc = struct.unpack_from("<%dI" % (crun + 1), page, 0)
        bx_base = 4 * (crun + 1)
        for i in range(crun):
            b_off = page[bx_base + 13 * i]
            istd = 0
            if b_off:
                p = b_off * 2
                cb = page[p]
                istd = struct.unpack_from("<H", page, p + (2 if cb == 0 else 1))[0]
            runs.append((rgfc[i], rgfc[i + 1], istd))
    runs.sort()
    return runs


def _clean_text(raw: str) -> str:
    """Drop field codes (keep a field's displayed result), pictures, and other control
    characters; keep \\r (paragraph end) and \\x0c (page break) for the caller."""
    out = []
    stack: list[str] = []  # "code" or "result" for each open field
    for ch in raw:
        if ch == "\x13":
            stack.append("code")
        elif ch == "\x14":
            if stack:
                stack[-1] = "result"
        elif ch == "\x15":
            if stack:
                stack.pop()
        elif stack and stack[-1] == "code":
            continue
        elif ch in ("\r", "\x0c"):
            out.append(ch)
        elif ch == "\x0b":
            out.append("\u2028")  # soft line break: kept so an inline heading can be split out
        elif ch in ("\x07", "\t"):
            out.append(" ")
        elif ch == "\x1e":
            out.append("-")
        elif ch < " " or ch in ("\x1f", "￼"):
            continue
        else:
            out.append(ch)
    return "".join(out)


def read_doc(path: Path) -> list[Para | None]:
    """Paragraphs of the main story in order, with `None` marking each manual page break."""
    try:
        import olefile
    except ImportError as exc:
        raise DocError("olefile is required: uv run --with olefile python ...") from exc
    try:
        ole = olefile.OleFileIO(str(path))
    except Exception as exc:
        raise DocError(f"not a Word 97-2003 .doc file: {exc}") from exc
    wd = ole.openstream("WordDocument").read()
    flags = struct.unpack_from("<H", wd, 0x0A)[0]
    tbl = ole.openstream("1Table" if flags & 0x0200 else "0Table").read()

    ccp_text = struct.unpack_from("<I", wd, 0x4C)[0]
    pieces = _pieces(wd, tbl)
    raw = _story_text(wd, pieces, ccp_text)
    names = _style_names(wd, tbl)
    runs = _paragraph_istds(wd, tbl)
    run_starts = [r[0] for r in runs]

    def cp_to_fc(cp: int) -> int:
        for cp0, cp1, off, unit in pieces:
            if cp0 <= cp < cp1:
                return off + (cp - cp0) * unit
        return -1

    def style_at(cp: int) -> str:
        fc = cp_to_fc(cp)
        i = bisect.bisect_right(run_starts, fc) - 1
        if i < 0 or fc >= runs[i][1]:
            return ""
        istd = runs[i][2]
        return names[istd] if istd < len(names) else ""

    result: list[Para | None] = []
    start = 0
    for m in re.finditer("\r", raw):
        end = m.start()
        style = style_at(end)  # the style lives on the paragraph mark
        segment = raw[start:end]
        text = _clean_text(segment)
        parts = text.split("\x0c")
        for k, part in enumerate(parts):
            if k > 0:
                result.append(None)
            part = re.sub(r"[  ]+", " ", part).strip()
            if part:
                result.append(Para(part, style))
            elif "\x01" in segment and len(parts) == 1:
                # A picture-only paragraph (e.g. a cover image): no text, but the page
                # it sits on is real, so it must not be mistaken for a break artifact.
                result.append(Para("", "picture"))
        start = end + 1
    tail = _clean_text(raw[start:]).strip()
    if tail:
        result.append(Para(tail, ""))
    return result


# ── .docx reading ───────────────────────────────────────────────────────────────

VERSE_JOINER = " * "  # the library's convention: one block per verse, hemistichs joined by " * "


def read_docx(path: Path) -> list[Para | None]:
    """Same output as read_doc for a .docx: paragraphs in order (style names kept), `None`
    at each page boundary (a manual page break, or a next-page section break).

    Tables are read in place, in document order. A row of several cells is a verse -- the
    non-empty cells joined with " * " -- and a row with one cell is plain text. Skipping
    tables would silently drop the poems of a diwan (89% of one real book's text)."""
    try:
        import docx
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as exc:
        raise DocError("python-docx is required: uv run --with olefile --with python-docx ...") from exc
    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise DocError(f"not a .docx file: {exc}") from exc

    w_t, w_tab, w_br = qn("w:t"), qn("w:tab"), qn("w:br")
    result: list[Para | None] = []
    # Word starts no extra page for a manual page break that directly follows a next-page
    # section break (only empty paragraphs between): the section already began a new page.
    # Counting both put a blank page into one real book that Word itself doesn't show,
    # shifting every later page number by one.
    after_section_break = False

    def paragraph(p_el) -> None:
        para = Paragraph(p_el, document)
        style = para.style.name if para.style is not None else ""
        segments = [""]
        for run in para.runs:
            for child in run._element:
                if child.tag == w_t:
                    segments[-1] += child.text or ""
                elif child.tag == w_tab:
                    segments[-1] += " "
                elif child.tag == w_br:
                    if child.get(qn("w:type")) == "page":
                        segments.append("")
                    else:
                        segments[-1] += "\u2028"  # soft line break, kept for heading recovery
        has_picture = bool(p_el.findall(".//" + qn("w:drawing")) or p_el.findall(".//" + qn("w:pict")))
        nonlocal after_section_break
        for k, seg in enumerate(segments):
            if k > 0:
                if after_section_break:
                    after_section_break = False
                else:
                    result.append(None)
            seg = re.sub(r"[ \u00a0]+", " ", seg).strip(" ")
            if seg.strip():
                result.append(Para(seg.strip(), style))
                after_section_break = False
            elif has_picture and len(segments) == 1:
                result.append(Para("", "picture"))
                after_section_break = False
        ppr = p_el.find(qn("w:pPr"))
        sect = ppr.find(qn("w:sectPr")) if ppr is not None else None
        if sect is not None:
            kind = sect.find(qn("w:type"))
            if kind is None or kind.get(qn("w:val")) != "continuous":
                result.append(None)  # a next-page section break starts a new page
                after_section_break = True

    def table(tbl_el) -> None:
        nonlocal after_section_break
        after_section_break = False
        tbl = Table(tbl_el, document)
        for row in tbl.rows:
            seen, cells = set(), []
            for cell in row.cells:
                if id(cell._tc) in seen:  # a merged cell is reported once per grid column
                    continue
                seen.add(id(cell._tc))
                text = " ".join(t.strip() for t in cell.text.split("\n") if t.strip())
                if text:
                    cells.append(re.sub(r"[ \u00a0]+", " ", text))
            if cells:
                result.append(Para(VERSE_JOINER.join(cells), "table-verse" if len(cells) > 1 else "table-text"))

    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            paragraph(child)
        elif child.tag == qn("w:tbl"):
            table(child)
    return result


def read_any(path: Path) -> list[Para | None]:
    return read_docx(path) if path.suffix.lower() == ".docx" else read_doc(path)


# ── Recover printed page numbers + headings from the book's own table of contents ──

_TOC_LINE_RE = re.compile(r"^\(?(.*?)\s+(\d{1,4})$")
_LEAD_NUM_RE = re.compile(r"^[\s(\[]*\d+\s*[ـ\-–.)]\s*")


def _key(text: str) -> str:
    from app.services.arabic import normalize

    text = text.replace("\u2028", " ")
    text = re.sub(r"\(\s*\d+\s*\)", " ", text)  # footnote markers such as "(4)"
    return re.sub(r"\s+", " ", normalize(_LEAD_NUM_RE.sub("", text))).strip("( [")


def anchor_toc(pages: list[list[Para]]):
    """Use the book's own TOC (paragraphs styled "TOC n" ending in a printed page number)
    to find, for each entry, the body paragraph it points at. Returns (anchors, toc_pages)
    where anchors is a list of (sequence, printed_number) in reading order. Matched
    paragraphs are flagged as headings, and a title the TOC wrapped over two lines is
    merged back into one heading."""
    toc_pages = [k for k, ps in enumerate(pages, 1) if any(p.style.upper().startswith("TOC") for p in ps)]
    if not toc_pages:
        return [], [], []
    first_toc = toc_pages[0]
    entries = []
    pending: list[str] = []  # unnumbered first lines of a wrapped TOC 2 title
    for k in toc_pages:
        for p in pages[k - 1]:
            if p.style.upper().startswith("TOC"):
                text = p.text.replace("\u2028", " ").strip()
                if p.style.upper().startswith("TOC 1"):
                    pending = []
                    continue
                m = _TOC_LINE_RE.match(text)
                if m and _key(m.group(1)):
                    title = " ".join([*pending, m.group(1).strip()])
                    entries.append((_key(m.group(1)), int(m.group(2)), title))
                    pending = []
                elif text:
                    pending.append(text)

    anchors: list[tuple[int, int]] = []
    last_page, last_idx = 1, -1
    prev_match: Para | None = None
    unmatched: list[tuple[str, int, str]] = []
    for key, number, title in entries:
        probe = key[:22]
        found = None
        for k in range(last_page, first_toc):
            start = last_idx + 1 if k == last_page else 0
            for i in range(start, len(pages[k - 1])):
                p = pages[k - 1][i]
                if p.style.lower().startswith("rfdfootnote") or SEPARATOR_RE.match(p.text):
                    continue
                if _key(p.text).startswith(probe):
                    found = (k, i, p)
                    break
            if found:
                break
        if not found:
            unmatched.append((probe, number, title))
            continue
        k, i, p = found
        anchors.append((k, number))
        last_page, last_idx = k, i
        # A title the TOC wrapped over two lines matches two adjacent body paragraphs.
        if prev_match is not None and pages[k - 1] and i > 0 and pages[k - 1][i - 1] is prev_match:
            prev_match.text = f"{prev_match.text} {p.text}"
            p.text = ""
        else:
            p.heading = True
            prev_match = p
    anchors = _drop_outlier_anchors(anchors)
    leftovers, soft = _fuzzy_headings(pages, anchors, unmatched)
    anchors = _drop_outlier_anchors(sorted({*anchors, *soft}))
    _complete_chapter_titles(pages, toc_pages)
    recovered = _recover_missing(pages, anchors, leftovers, toc_pages[0])
    return anchors, toc_pages, recovered


def _complete_chapter_titles(pages, toc_pages) -> None:
    """A chapter title that wraps ("الفصل الأول : في مجربات ..." + "... لطلب الرزق") is one
    Heading paragraph plus the paragraphs carrying its remaining lines. The index's TOC 1
    lines hold the full text, so pull in any directly following paragraph that appears in
    them."""
    blob = " ".join(
        _key(p.text) for k in toc_pages for p in pages[k - 1] if p.style.upper().startswith("TOC 1")
    )
    if not blob:
        return
    for k in range(1, toc_pages[0]):
        ps = pages[k - 1]
        for i, p in enumerate(ps):
            if not (p.text and p.style.lower().startswith("heading")):
                continue
            j = i + 1
            while j < len(ps):
                q = ps[j]
                qk = _key(q.text)
                if q.heading or len(qk) < 8 or qk[:24] not in blob:
                    break
                p.text = f"{p.text} {q.text}"
                q.text = ""
                j += 1


def _fuzzy_headings(pages, anchors, unmatched) -> None:
    """Index entries the exact prefix match missed (a typo or spelling difference between
    the index and the body). Done after the main pass so a wrong guess cannot derail it:
    look only at heading-styled paragraphs on the few pages the printed number points to,
    and accept a close-enough match."""
    from difflib import SequenceMatcher

    by_number = sorted((num, seq - num) for seq, num in anchors)
    leftovers = []
    soft: list[tuple[int, int]] = []  # (position, printed number) from these matches
    for probe, number, title in unmatched:
        before = [off for num, off in by_number if num <= number]
        offset = before[-1] if before else (by_number[0][1] if by_number else 0)
        expected = number + offset
        best, target, target_k = 0.0, None, 0
        for k in range(max(1, expected - 1), min(len(pages), expected + 2) + 1):
            for p in pages[k - 1]:
                if p.heading or not p.text or not _HEADING_LIKE_RE.match(p.style):
                    continue
                score = SequenceMatcher(None, probe, _key(p.text)[: len(probe)]).ratio()
                if score > best:
                    best, target, target_k = score, p, k
        if target is not None and best >= 0.7:
            target.heading = True
            soft.append((target_k, number))
            continue
        # The index often words an entry differently from the heading printed in the body
        # ("سياسة معاوية : الارهاب والتجويع" vs "أ ـ الإرهاب والتجويع"). Compare by shared
        # words instead, still limited to heading-styled paragraphs on the pages around
        # where the printed number points.
        words = _words(title)
        best, target, target_k = 0.0, None, 0
        for k in range(max(1, expected - 1), min(len(pages), expected + 1) + 1):
            for p in pages[k - 1]:
                if p.heading or not p.text or not _HEADING_LIKE_RE.match(p.style):
                    continue
                other = _words(p.text)
                shared = len(words & other)
                score = shared / min(len(words), len(other)) if words and other else 0.0
                if shared >= 2 and score > best:
                    best, target, target_k = score, p, k
        if target is not None and best >= 0.6:
            target.heading = True
            soft.append((target_k, number))
        else:
            leftovers.append((probe, number, title, expected))
    return leftovers, soft


def _words(text: str) -> set[str]:
    return {w for w in _key(text).split() if len(w) >= 3}


def _recover_missing(pages, anchors, leftovers, first_toc):
    """Index entries with no heading paragraph of their own. Each is one of:
    a continuation line of a title the index wrapped (merged into that heading), a title
    glued into another paragraph after a soft line break (that paragraph is split so the
    heading lands where it really is), or absent from the body (a heading with the
    index's title is inserted at the top of the page the number points to). Returns
    (printed number, title, how) for each, so a caller can report them."""
    from difflib import SequenceMatcher

    report = []
    for probe, number, title, expected in leftovers:
        lo, hi = max(1, expected - 1), min(first_toc - 1, expected + 2)
        done = False
        # (a) continuation of a wrapped title already present as a heading
        for k in range(lo, hi + 1):
            ps = pages[k - 1]
            for i, q in enumerate(ps):
                if q.heading and probe in _key(q.text):
                    done = True
                elif (i > 0 and ps[i - 1].heading and q.text and not q.heading
                      and (_key(q.text).startswith(probe[:14])
                           or SequenceMatcher(None, probe, _key(q.text)[:len(probe)]).ratio() >= 0.75)):
                    ps[i - 1].text = f"{ps[i - 1].text} {q.text}"
                    q.text = ""
                    done = True
                if done:
                    break
            if done:
                break
        if done:
            report.append((number, title, "continuation of the previous heading"))
            continue
        # (b) heading glued into a longer paragraph after a soft line break
        for k in range(lo, hi + 1):
            ps = pages[k - 1]
            for i, q in enumerate(ps):
                if q.heading or "\u2028" not in q.text or q.style.lower().startswith("rfdfootnote"):
                    continue
                segs = q.text.split("\u2028")
                for j, seg in enumerate(segs):
                    sk = _key(seg)
                    if sk and (sk.startswith(probe[:16])
                               or SequenceMatcher(None, probe, sk[:len(probe)]).ratio() >= 0.8):
                        parts = []
                        if segs[:j]:
                            parts.append(Para(" ".join(x.strip() for x in segs[:j] if x.strip()), q.style))
                        parts.append(Para(seg.strip(), q.style, heading=True))
                        if segs[j + 1:]:
                            parts.append(Para(" ".join(x.strip() for x in segs[j + 1:] if x.strip()), q.style))
                        ps[i:i + 1] = [x for x in parts if x.text]
                        done = True
                        break
                if done:
                    break
            if done:
                break
        if done:
            report.append((number, title, "split out of the paragraph it was glued into"))
            continue
        # (c) not in the body at all: heading with the index's title at the page's top
        target = max(1, min(first_toc - 1, expected))
        pages[target - 1].insert(0, Para(title, "toc-inserted", heading=True))
        report.append((number, title, f"inserted at the top of page {target}"))
    return report


_HEADING_LIKE_RE = re.compile(r"^(heading|title|rfdcenterbold|rfdbold)", re.IGNORECASE)


def _drop_outlier_anchors(anchors: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """A TOC title can match the wrong body paragraph (a similar heading elsewhere on a
    neighbouring page). Such a mismatch shows up as one anchor whose offset differs from
    both neighbours while they agree with each other -- a real pagination change would
    persist, so an isolated deviation is dropped."""
    kept = list(anchors)
    i = 1
    while 0 < i < len(kept) - 1:
        prev_off = kept[i - 1][0] - kept[i - 1][1]
        off = kept[i][0] - kept[i][1]
        next_off = kept[i + 1][0] - kept[i + 1][1]
        if off != prev_off and prev_off == next_off:
            del kept[i]
        else:
            i += 1
    return kept


def artifact_pages(pages: list[list[Para]], anchors: list[tuple[int, int]]) -> set[int]:
    """Positions of completely empty pages that are page-break artifacts, not printed pages.

    Two back-to-back break characters leave an empty "page" in the split. Sometimes that
    is a real blank page in the printed book (chapter separators -- the numbering keeps
    counting it) and sometimes an artifact (numbering skips it). The book's TOC decides:
    between two consecutive anchors, if position runs ahead of the printed number by N
    more than before, exactly N empty pages in that stretch are artifacts."""
    drop: set[int] = set()
    for (sa, na), (sb, nb) in zip(anchors, anchors[1:]):
        extra = (sb - nb) - (sa - na)
        if extra <= 0:
            continue
        empties = [k for k in range(sa + 1, sb) if not pages[k - 1]]
        drop.update(empties[:extra])
    return drop


def page_labels(count: int, anchors: list[tuple[int, int]]) -> list[int]:
    """Printed number for every page: seq minus the offset of the latest anchor at or
    before it (the first anchor's offset for pages before it)."""
    if not anchors:
        return list(range(1, count + 1))
    offsets = [(seq, seq - num) for seq, num in anchors]
    labels = []
    j = 0
    for seq in range(1, count + 1):
        while j + 1 < len(offsets) and offsets[j + 1][0] <= seq:
            j += 1
        labels.append(seq - offsets[j][1])
    return labels


# ── readme.txt (rafed.net downloads) → title / author / publication details ─────────

_README_KEYS = {
    "المؤلف": "author", "الناشر": "publisher", "الطبعة": "edition",
    "تاريخ النشر": "publicationYear", "المحقق": "editor",
}


def read_readme(path: Path) -> dict[str, str]:
    """Title (first real line) and the labelled fields the download's readme carries.
    A publication date of 0 means "unknown" and is dropped; a Hijri year has its ".ق"
    suffix trimmed ("1432 هـ.ق" -> "1432 هـ")."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith(("!!", "_", "http")):
            continue
        m = re.match(r"^([^:：]+?)\s*[:：]\s*(.+)$", line)
        if m and m.group(1).strip() in _README_KEYS:
            out[_README_KEYS[m.group(1).strip()]] = m.group(2).strip()
        elif "title" not in out and not m:
            out["title"] = line
    year = out.get("publicationYear", "")
    if year:
        year = re.sub(r"\.\s*ق\b", "", year).strip()
        if re.match(r"^0+\b", year):
            out.pop("publicationYear")
        else:
            out["publicationYear"] = year
    return out


# ── Build the v2 book ─────────────────────────────────────────────────────────


def _split_pages(items: list[Para | None]) -> list[list[Para]]:
    pages: list[list[Para]] = [[]]
    for it in items:
        if it is None:
            pages.append([])
        else:
            pages[-1].append(it)
    return _split_merged_pages(pages)


def _split_merged_pages(pages: list[list[Para]]) -> list[list[Para]]:
    """A Word page holding two footnote bars is two printed pages laid out without a break
    character between them: [text][footnotes][text][footnotes]. Footnotes end a printed
    page, so the first body paragraph after the first page's footnotes starts a new one.
    Requiring the second bar keeps a footnote that merely continues in a plain style (no
    bar after it) from being mistaken for a new page."""
    out: list[list[Para]] = []
    for ps in pages:
        bars = [i for i, p in enumerate(ps) if SEPARATOR_RE.match(p.text)]
        cuts = []
        for a, b in zip(bars, bars[1:]):
            j = next((i for i in range(a + 1, b)
                      if ps[i].text and not ps[i].style.lower().startswith("rfdfootnote")), None)
            if j is not None:
                cuts.append(j)
        start = 0
        for c in cuts:
            out.append(ps[start:c])
            start = c
        out.append(ps[start:])
    return out


def _strip_angles(s: str) -> str:
    return s.replace("<", "").replace(">", "")


def build_abx(pages, title, author, front_pages, labels, heading_re) -> str:
    lines = [
        "checksum-not-applicable",
        f"< اسم الكتاب > {_strip_angles(title).strip()} < / اسم الكتاب >",
        f"< اسم المؤلف > {_strip_angles(author).strip()} < / اسم المؤلف >",
        "< الكتاب >",
    ]
    for k, paras in enumerate(pages, start=1):
        label = f"تعريف الكتاب {k}" if k <= front_pages else str(labels[k - 1])
        lines.append(f"< صفحة > {label} < / صفحة >")
        in_footnotes = False
        for p in paras:
            text = _strip_angles(p.text).replace("\u2028", " ").strip()
            if not text:
                continue
            if SEPARATOR_RE.match(text):
                in_footnotes = True
                continue
            if in_footnotes or p.style.lower().startswith("rfdfootnote"):
                lines.append(f"< هامش > {text} < / هامش >")
            elif p.heading or heading_re.match(p.style):
                lines += ["< فهرس الموضوعات >", text, "< / فهرس الموضوعات >"]
            else:
                lines.append(text)
    lines.append("< / الكتاب >")
    return "\n".join(lines)


def convert_doc(path, title, author, front_pages, first_printed, heading_re, book_id="900001",
                use_toc=True, extra_metadata=None, blank_pages=()):
    items = read_any(path)
    pages = _split_pages(items)
    # A blank page the source file cannot show (Word pushes a break paragraph onto a fresh
    # page when the page before it is full, leaving nothing in the file) -- stated by the
    # caller, given as its final page number. Held as a picture-style paragraph so it is a
    # real, never-dropped page.
    blanks = sorted({int(b) for b in blank_pages})
    for b in blanks:
        pages.insert(min(max(b, 1), len(pages) + 1) - 1, [Para("", "picture")])
    anchors, recovered = [], []
    if use_toc:
        anchors, _, recovered = anchor_toc(pages)
        # The index was numbered without those blank pages; restate its numbers in the
        # book's real numbering so every later page still agrees with its own entry.
        anchors = [(seq, num + sum(1 for b in blanks if b < seq)) for seq, num in anchors]
    if anchors:
        drop = artifact_pages(pages, anchors)
        if drop:
            shift = lambda pos: pos - sum(1 for d in drop if d < pos)  # noqa: E731
            anchors = [(shift(seq), num) for seq, num in anchors]
            pages = [pg for k, pg in enumerate(pages, 1) if k not in drop]
        labels = page_labels(len(pages), anchors)
    else:
        labels = [first_printed + (k - front_pages - 1) for k in range(1, len(pages) + 1)]
    abx = build_abx(pages, title, author, front_pages, labels, heading_re)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"{book_id}.abx"
        src.write_text(abx, encoding="utf-8")
        content = conv.convert(src, book_id)

    # The Shamela classifier infers the front/main split from label patterns; here the
    # split is known exactly, so set it directly rather than trusting the heuristic.
    front = 0
    for page in content["pages"]:
        if page["sequence"] <= front_pages:
            front += 1
            page["pageType"] = "frontMatter"
            page["pageNumber"] = f"0.{front}"
        else:
            page["pageType"] = "main"
            printed = page.get("printedPage")
            page["pageNumber"] = str(printed) if printed is not None else str(page["sequence"])
    numbers = {p["id"]: p["pageNumber"] for p in content["pages"]}
    for entry in content["toc"]:
        entry["pageNumber"] = numbers[entry["pageId"]]
    content["metadata"].update({k: v for k, v in (extra_metadata or {}).items() if v})
    content["_anchors"] = anchors
    content["_recovered"] = recovered
    return content, items


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("doc", type=Path)
    ap.add_argument("--title", help="book title (default: the first line of --readme)")
    ap.add_argument("--author", default="")
    ap.add_argument("--readme", type=Path, help="the download's readme.txt: fills title, author, "
                    "publisher, edition and year unless given explicitly")
    ap.add_argument("--publisher", default="")
    ap.add_argument("--edition", default="")
    ap.add_argument("--year", default="", help="publication year (metadata.publicationYear)")
    ap.add_argument("--death", default="", help="author's death date (metadata.authorDeath)")
    ap.add_argument("--editor", default="")
    ap.add_argument("--isbn", default="")
    ap.add_argument("--language", default="", choices=["", "ar", "fa"])
    ap.add_argument("--volume", default="", help="volume number (digits only)")
    ap.add_argument("--notes", default="")
    ap.add_argument("--blank-page", default="", help="comma-separated printed page numbers that are "
                    "blank in the original but invisible in the file (inserted as empty pages)")
    ap.add_argument("--front-pages", type=int, default=0, help="leading pages that are front matter")
    ap.add_argument("--first-printed", type=int, default=1, help="printed number of the first main page "
                    "(only used when the book's own TOC can't be used)")
    ap.add_argument("--no-toc", action="store_true",
                    help="don't derive page numbers/headings from the book's own TOC pages")
    ap.add_argument("--heading-styles", help="comma-separated style names to treat as headings "
                    "(default: any style starting with Heading or Title)")
    ap.add_argument("--book-id", default="900001")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--dump-styles", action="store_true", help="list paragraph styles and exit")
    ap.add_argument("--preview", type=int, default=0, help="print N pages of blocks")
    args = ap.parse_args()

    try:
        if args.dump_styles:
            items = read_any(args.doc)
            count = collections.Counter(p.style for p in items if p)
            samples: dict[str, str] = {}
            for p in items:
                if p and p.style not in samples:
                    samples[p.style] = p.text[:60]
            for style, n in count.most_common():
                print(f"{n:>6}  {style or '(none)':<30} e.g. {samples[style]}")
            return 0

        readme = read_readme(args.readme) if args.readme else {}
        title = args.title or readme.get("title")
        if not title:
            raise DocError("give --title (or a --readme that starts with the title)")
        author = args.author or readme.get("author", "")
        extra = {
            "publisher": args.publisher or readme.get("publisher", ""),
            "edition": args.edition or readme.get("edition", ""),
            "publicationYear": args.year or readme.get("publicationYear", ""),
            "authorDeath": args.death, "editor": args.editor, "isbn": args.isbn,
            "language": args.language, "volume": args.volume, "notes": args.notes,
        }
        if args.heading_styles:
            wanted = {s.strip() for s in args.heading_styles.split(",")}
            heading_re = re.compile("^(" + "|".join(re.escape(s) for s in wanted) + ")$")
        else:
            heading_re = HEADING_STYLE_RE
        content, _ = convert_doc(
            args.doc, title, author, args.front_pages, args.first_printed,
            heading_re, args.book_id, use_toc=not args.no_toc, extra_metadata=extra,
            blank_pages=[x for x in args.blank_page.split(",") if x.strip()],
        )
        anchors = content.pop("_anchors")
        recovered = content.pop("_recovered")
    except (DocError, conv.ConversionError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2

    pages, toc = content["pages"], content["toc"]
    kinds = collections.Counter(b["type"] for p in pages for b in p["blocks"])
    main_pages = [p for p in pages if p["pageType"] == "main"]
    print(f"title:    {content['title']}")
    print(f"author:   {content['author'] or '(none)'}")
    if content["metadata"]:
        print(f"metadata: {content['metadata']}")
    print(f"pages:    {len(pages)} ({len(pages) - len(main_pages)} front matter, {len(main_pages)} main)")
    if main_pages:
        print(f"printed:  {main_pages[0]['pageNumber']} … {main_pages[-1]['pageNumber']}")
    print(f"blocks:   {dict(kinds)}")
    print(f"sections: {len(toc)}")
    if anchors:
        offs = []
        for seq, num in anchors:
            if not offs or offs[-1][2] != seq - num:
                offs.append((seq, num, seq - num))
        print(f"page numbers from the book's own TOC: {len(anchors)} anchors; offset (position - printed) "
              f"changes: " + ", ".join(f"pos {a}→p.{b} (offset {c})" for a, b, c in offs))
    else:
        print("page numbers: sequential (no usable TOC in the file)")
    if recovered:
        print(f"index entries added without their own heading paragraph ({len(recovered)}):")
        for number, title, how in recovered:
            print(f"   p.{number}  {title[:55]}  -> {how}")
    for e in toc[:12]:
        print(f"   {e['order']:>3}. {e['title'][:60]}  (p. {e['pageNumber']})")
    if len(toc) > 12:
        print(f"   … {len(toc) - 12} more")
    for page in pages[: args.preview]:
        print(f"\n── page {page['pageNumber']} ({page['pageType']}) ──")
        for b in page["blocks"][:10]:
            print(f"  [{b['type'][:4]}] {b['text'][:100]}")

    issues = val.validate(content)
    errors = [i for i in issues if i.severity == "error"]
    print(f"\nvalidation: {len(errors)} error(s), {len(issues) - len(errors)} warning(s)")
    for i in issues:
        print(f"  {'✗' if i.severity == 'error' else '⚠'} {i.code}: {i.detail}")

    # Named after the book, not the input file (a download is often just "book.doc").
    safe_title = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", content["title"]).strip(" .")[:120] or args.doc.stem
    out = args.out or args.doc.with_name(f"{safe_title}.json")
    out.write_text(json.dumps(content, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"\nwrote {out}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
