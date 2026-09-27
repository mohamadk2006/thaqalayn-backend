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


def is_footnote_style(style: str) -> bool:
    """rfdFootnote0, rfdFootnoteCenter, rfdPoemFootnoteCenter, ...: a poem in the footnotes
    is still a footnote. Only "starts with rfdFootnote" was checked, and 28 of one real
    book's 37 footnote poems came out as main text."""
    return "footnote" in style.lower()


def is_poem_style(style: str) -> bool:
    return "poem" in style.lower()


# rafed.net books set honorifics and Qur'an brackets as plain characters in a symbol font
# (ALAEM, "Rafed Alaem"), through a character style named for it (rfdAlaem): read as text
# they were "أمير المؤمنين 7", "النبي 9". Worked out from where each one stands in twelve
# real books (7 after علي and الحسين 1,000+ times, 3 after فاطمة and زينب, 8 after "الحسن
# والحسين", ":" after "أهل البيت", "2 وأرضاه", 4 after "العائلة الكريمة ... جميعاً") and,
# for "1", from the library's own text of the same sentence ("من خط الشهيد قدس سره").
HONORIFICS = {
    "7": "عليه السلام",
    "9": "صلى الله عليه وآله",
    "6": "صلى الله عليه وآله وسلم",
    "3": "عليها السلام",
    "8": "عليهما السلام",
    ":": "عليهم السلام",
    "2": "رضي الله عنه",
    "4": "رضي الله عنهم",
    "1": "قدس سره",
    ";": "رحمه الله",
    "(": "\ufd3f",  # ornate brackets round a Qur'an quote, as the library's books have them
    ")": "\ufd3e",
}
_HONORIFIC_MARK = {ch: chr(0xE100 + i) for i, ch in enumerate(HONORIFICS)}
_HONORIFIC_TEXT = {mark: HONORIFICS[ch] for ch, mark in _HONORIFIC_MARK.items()}
_HONORIFIC_RE = re.compile("[" + "".join(_HONORIFIC_MARK.values()) + "]")


def is_honorific_style(style: str) -> bool:
    return "alaem" in style.lower()


def _honorific_marks(text: str) -> str:
    """A symbol-font run's characters, each as a placeholder until the paragraph is done."""
    return "".join(_HONORIFIC_MARK.get(ch, ch) for ch in text)


def _spell_honorifics(text: str) -> str:
    def one(m):
        phrase = _HONORIFIC_TEXT[m.group(0)]
        if phrase in ("\ufd3f", "\ufd3e"):
            return phrase
        before = text[m.start() - 1] if m.start() else ""
        after = text[m.end()] if m.end() < len(text) else ""
        lead = " " if before and not before.isspace() and before not in "(«[\ufd3f" else ""
        trail = " " if after and (after.isalnum() or after in "(«\ufd3f") else ""
        return lead + phrase + trail
    return _HONORIFIC_RE.sub(one, text)


def _finish(items: list) -> list:
    """Honorifics spelt out, and every text in one Unicode form (NFC): Word files mix
    composed and decomposed Arabic letters (hamza on alef, madda), which the validator flags
    and which look alike but compare differently."""
    import unicodedata

    for p in items:
        if p is not None and p.text:
            p.text = unicodedata.normalize("NFC", _spell_honorifics(p.text))
    return items


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


def _inline_section_marks(wd: bytes, tbl: bytes) -> set[int]:
    """CPs of the section marks that start no new page. In the text a section mark is the
    same \\x0c as a manual page break, but a section whose break kind (sprmSBkc) is
    "continuous" or "new column" goes on on the same page. Counting those as pages put
    dozens of empty pages into real books that Word doesn't show."""
    fc, lcb = struct.unpack_from("<II", wd, 0x00CA)
    if lcb < 4 + 4 + 12:
        return set()
    n = (lcb - 4) // 16
    cps = struct.unpack_from("<%dI" % (n + 1), tbl, fc)
    marks = set()
    for i in range(1, n):  # the break kind of section i decides the mark ending section i-1
        fc_sepx = struct.unpack_from("<I", tbl, fc + 4 * (n + 1) + 12 * i + 2)[0]
        kind = 2  # new page, when the section says nothing
        if fc_sepx != 0xFFFFFFFF and fc_sepx + 2 <= len(wd):
            cb = struct.unpack_from("<H", wd, fc_sepx)[0]
            grpprl = wd[fc_sepx + 2:fc_sepx + 2 + cb]
            pos = 0
            while pos + 2 <= len(grpprl):
                sprm = struct.unpack_from("<H", grpprl, pos)[0]
                pos += 2
                spra = sprm >> 13
                if sprm == 0x3009 and pos < len(grpprl):
                    kind = grpprl[pos]
                if spra == 6:
                    pos += (grpprl[pos] if pos < len(grpprl) else 0) + 1
                else:
                    pos += {0: 1, 1: 1, 2: 2, 3: 4, 4: 2, 5: 2, 7: 3}[spra]
        if kind in (0, 1):
            marks.add(cps[i] - 1)
    return marks


def _character_styles(wd: bytes, tbl: bytes):
    """Sorted (fc_start, fc_end, istd) of text runs formatted with a character style
    (sprmCIstd), from the CHPX bin table."""
    fc, lcb = struct.unpack_from("<II", wd, 0x00FA)
    plc = tbl[fc:fc + lcb]
    n = (lcb - 4) // 8
    runs = []
    for pn in struct.unpack_from("<%dI" % n, plc, 4 * (n + 1)):
        page = wd[pn * 512:(pn + 1) * 512]
        crun = page[511]
        rgfc = struct.unpack_from("<%dI" % (crun + 1), page, 0)
        for i in range(crun):
            off = page[4 * (crun + 1) + i] * 2
            if not off:
                continue
            grpprl = page[off + 1:off + 1 + page[off]]
            pos = 0
            while pos + 2 <= len(grpprl):
                sprm = struct.unpack_from("<H", grpprl, pos)[0]
                pos += 2
                spra = sprm >> 13
                if sprm == 0x4A30 and pos + 2 <= len(grpprl):  # sprmCIstd
                    runs.append((rgfc[i], rgfc[i + 1], struct.unpack_from("<H", grpprl, pos)[0]))
                if spra == 6:
                    pos += (grpprl[pos] if pos < len(grpprl) else 0) + 1
                else:
                    pos += {0: 1, 1: 1, 2: 2, 3: 4, 4: 2, 5: 2, 7: 3}[spra]
    return sorted(runs)


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
        elif ch == "\x07":
            out.append("\x07")  # table cell end: read_doc lays the cells out
        elif ch == "\t":
            out.append(" ")
        elif ch == "\x1e":
            out.append("-")
        elif ch < " " or ch in ("\x1f", "￼"):
            continue
        else:
            out.append(ch)
    return "".join(out)


# A table cell longer than this is prose, not half a verse.
HEMISTICH_MAX = 90


def _is_poem_table(cells: list[tuple[str, str]]) -> bool:
    """Half-verse cells: short, and set in a poem style -- or in plain Normal, as many of
    the books' poem tables are. Indexes are tables too ("للصحن العباسيّ | 223"), in their own
    styles and ending in page numbers; pairing their cells broke a real book's contents."""
    if not all(len(c) <= HEMISTICH_MAX for c, _ in cells):
        return False
    if any(is_poem_style(st) for _, st in cells):
        return True
    return all(st == "Normal" for _, st in cells) and not any(re.search(r"\d\W*$", c) for c, _ in cells)


def _table_paras(part: str, style: str, cell_styles: list[str]) -> list[Para]:
    """The table cells ending in this paragraph (each ends with \\x07; a row adds one more),
    then whatever follows the table. The books set a poem as a table of half-verse cells in
    reading order -- first half, second half, first half, ... -- so short cells are paired
    into one verse, "first * second", as a .docx poem table is. Glued together, a whole
    poem became one run-on paragraph."""
    *raw_cells, after = part.split("\x07")
    clean = lambda t: re.sub(r"[ \u00a0\u2028]+", " ", t).strip()  # noqa: E731
    cells = [(clean(c), st) for c, st in zip(raw_cells, cell_styles) if clean(c)]
    out: list[Para] = []
    if len(cells) >= 2 and _is_poem_table(cells):
        for i in range(0, len(cells), 2):
            pair = cells[i:i + 2]
            out.append(Para(VERSE_JOINER.join(c for c, _ in pair), pair[0][1]))
    else:
        out += [Para(c, st) for c, st in cells]
    if clean(after):
        out.append(Para(clean(after), style))
    return out


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
    # A mark of a section that goes on on the same page still ends its paragraph.
    inline = [cp for cp in _inline_section_marks(wd, tbl) if cp < len(raw) and raw[cp] == "\x0c"]
    if inline:
        chars = list(raw)
        for cp in inline:
            chars[cp] = "\r"
        raw = "".join(chars)
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

    # Honorifics: characters in the symbol-font character style become placeholders here,
    # where each one's position is still its CP.
    honorific_styles = {i for i, name in enumerate(names) if is_honorific_style(name)}
    if honorific_styles:
        char_runs = [r for r in _character_styles(wd, tbl) if r[2] in honorific_styles]
        char_starts = [r[0] for r in char_runs]
        chars = list(raw)
        for cp, ch in enumerate(chars):
            if ch in _HONORIFIC_MARK and char_runs:
                fc = cp_to_fc(cp)
                i = bisect.bisect_right(char_starts, fc) - 1
                if i >= 0 and fc < char_runs[i][1]:
                    chars[cp] = _HONORIFIC_MARK[ch]
        raw = "".join(chars)

    result: list[Para | None] = []
    start = 0
    for m in re.finditer("\r", raw):
        end = m.start()
        style = style_at(end)  # the style lives on the paragraph mark
        segment = raw[start:end]
        text = _clean_text(segment)
        parts = text.split("\x0c")
        # A table cell's mark is its own paragraph mark and carries its own style; the
        # paragraph mark at the end belongs to what follows the table (often a heading).
        cell_styles = iter([style_at(start + j) for j, ch in enumerate(segment) if ch == "\x07"])
        for k, part in enumerate(parts):
            if k > 0:
                result.append(None)
            if "\x07" in part:
                result.extend(_table_paras(part, style, [next(cell_styles, style) for _ in range(part.count("\x07"))]))
                continue
            part = re.sub(r"[ \u00a0]+", " ", part).strip()
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
    return _finish(result)


# ── .docx reading ───────────────────────────────────────────────────────────────

VERSE_JOINER = " * "  # the library's convention: one block per verse, hemistichs joined by " * "


# Word's saved layout is used for the pages when a file has at most one page break of
# its own per this many of Word's page marks: its text flows, and Word laid out the pages.
FLOWING_TEXT_RATIO = 10
_NOTE = "\ue000{}\ue001"  # a footnote reference's place in the text until its number is known
_NOTE_RE = re.compile(r"(\(?)\ue000(-?\d+)\ue001(\)?)")
_OWN_MARK = "\ue002"  # a footnote's own number, where Word shows it inside the note
_OWN_MARK_RE = re.compile(r"\(?\ue002\)?")
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"


def read_docx(path: Path) -> list[Para | None]:
    """Same output as read_doc for a .docx: paragraphs in order (style names kept), `None`
    at each page boundary.

    Where pages end: a book typed with a page break at the end of every printed page gives
    them itself (manual breaks and next-page section breaks). A book whose text just flows
    has no such breaks -- Word decides -- but Word records where each page began when it
    last saved the file (w:lastRenderedPageBreak); those marks are Word's own pagination,
    and a section that must start on an odd (even) page gets the blank page Word prints
    before it. One real series of books had no page break at all: read by breaks, a
    450-page book became 25 pages.

    Everything a paragraph shows is read, wherever Word nests it (links -- a Word TOC is
    all links --, tracked insertions, content controls, fields' results), but not text
    boxes, deleted text or hidden text. Real Word footnotes (Insert Footnote) are put at
    the bottom of the page that cites them, numbered on each page from 1 as Word does, the
    mark "(n)" in the text; list numbers Word generates ("1-", "أ-") are written out.

    Tables are read in place, in document order. A row of several cells is a verse -- the
    non-empty cells joined with " * " -- and a row with one cell is plain text. Skipping
    tables would silently drop the poems of a diwan (89% of one real book's text)."""
    try:
        import docx
        from docx.oxml.ns import qn
    except ImportError as exc:
        raise DocError("python-docx is required: uv run --with olefile --with python-docx ...") from exc
    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise DocError(f"not a .docx file: {exc}") from exc

    body = document.element.body
    W_T, W_TAB, W_BR, W_CR = qn("w:t"), qn("w:tab"), qn("w:br"), qn("w:cr")
    W_MARK, W_NOTE, W_NOTEREF = qn("w:lastRenderedPageBreak"), qn("w:footnoteReference"), qn("w:footnoteRef")
    W_R, W_RPR, W_VANISH, W_P, W_TBL, W_SDT = qn("w:r"), qn("w:rPr"), qn("w:vanish"), qn("w:p"), qn("w:tbl"), qn("w:sdt")
    SKIP = {qn("w:txbxContent"), _MC_FALLBACK, qn("w:del"), qn("w:instrText"), qn("w:delText"),
            qn("w:pPr"), W_RPR, qn("w:sym"), qn("w:moveFrom")}
    manual = sum(1 for br in body.iter(W_BR) if br.get(qn("w:type")) == "page")
    char_style_names: dict[str, str] = {}

    def char_style(style_id: str) -> str:
        if style_id not in char_style_names:
            try:
                style = document.styles.get_by_id(style_id, 2)  # 2: WD_STYLE_TYPE.CHARACTER
            except Exception:  # noqa: BLE001
                style = None
            char_style_names[style_id] = style.name if style is not None else style_id
        return char_style_names[style_id]
    marks = sum(1 for _ in body.iter(W_MARK))
    by_marks = marks > 0 and manual * FLOWING_TEXT_RATIO < marks

    def walk(el):
        """(kind, value) for what el shows, in reading order."""
        for child in el:
            tag = child.tag
            if tag in SKIP:
                continue
            if tag == W_R:
                rpr = child.find(W_RPR)
                if rpr is not None and rpr.find(W_VANISH) is not None:
                    continue  # hidden text
                rstyle = rpr.find(qn("w:rStyle")) if rpr is not None else None
                if rstyle is not None and is_honorific_style(char_style(rstyle.get(qn("w:val")))):
                    for kind, value in walk(child):
                        yield (kind, _honorific_marks(value)) if kind == "text" else (kind, value)
                    continue
            if tag == W_T:
                yield "text", child.text or ""
            elif tag == W_TAB:
                yield "text", " "
            elif tag == W_CR:
                yield "text", "\u2028"
            elif tag == W_BR:
                kind = child.get(qn("w:type"))
                yield ("page", None) if kind == "page" else ("text", "\u2028" if kind in (None, "textWrapping") else " ")
            elif tag == W_MARK:
                yield "mark", None
            elif tag == W_NOTE:
                yield "text", _NOTE.format(child.get(qn("w:id")))
            elif tag == W_NOTEREF:
                yield "text", _OWN_MARK
            else:
                yield from walk(child)

    notes = _docx_footnotes(document, walk)
    numbering = _DocxNumbering(document)
    restart_each_page = _footnotes_restart_each_page(document)

    result: list[Para | None] = []
    pending_notes: list[str] = []  # footnotes of the page being read, numbered
    note_count = 0
    physical_page = 1
    parity_needed: str | None = None  # "oddPage"/"evenPage": the next page must be one

    def number_notes(text: str) -> str:
        nonlocal note_count

        def one(m):
            nonlocal note_count
            if m.group(2) not in notes:
                return m.group(1) + m.group(3)
            note_count += 1
            body_text, marked = _OWN_MARK_RE.subn(f"({note_count})", notes[m.group(2)], count=1)
            if not marked:
                body_text = f"({note_count}) {body_text}"
            pending_notes.append(body_text)
            return f"({note_count})"
        return _NOTE_RE.sub(one, text)

    def end_page() -> None:
        """Close the page: its footnotes at the bottom, then the page boundary."""
        nonlocal note_count, physical_page, parity_needed
        result.extend(Para(n, "footnote text") for n in pending_notes)
        pending_notes.clear()
        if restart_each_page:
            note_count = 0
        result.append(None)
        physical_page += 1
        if parity_needed and (physical_page % 2 == 1) != (parity_needed == "oddPage"):
            result.append(None)  # the blank page Word prints so the section starts on the right side
            physical_page += 1
        parity_needed = None

    # A sectPr ends its section, but its w:type says how *that* section began; whether a
    # new page follows it is up to the next section's type.
    sections = list(body.iter(qn("w:sectPr")))
    following_type = {}
    for sect, following in zip(sections, sections[1:]):
        kind = following.find(qn("w:type"))
        following_type[id(sect)] = kind.get(qn("w:val")) if kind is not None else "nextPage"

    # Word starts no extra page for a manual page break that directly follows a next-page
    # section break (only empty paragraphs between): the section already began a new page.
    # Counting both put a blank page into one real book that Word itself doesn't show.
    after_section_break = False

    def paragraph(p_el, style: str) -> None:
        nonlocal after_section_break, parity_needed
        label = numbering.label(p_el)
        segments = [""]
        for kind, value in walk(p_el):
            if kind == "text":
                segments[-1] += value
            elif (kind == "mark") == by_marks and kind in ("mark", "page"):
                segments.append("")
        has_picture = bool(p_el.findall(".//" + qn("w:drawing")) or p_el.findall(".//" + qn("w:pict")))
        for k, seg in enumerate(segments):
            if k > 0:
                if after_section_break and not by_marks:
                    after_section_break = False
                else:
                    end_page()
            seg = re.sub(r"[ \u00a0]+", " ", seg).strip(" ")
            if k == 0 and label and seg.strip():
                seg = f"{label} {seg.strip()}"
            if seg.strip():
                result.append(Para(number_notes(seg.strip()), style))
                after_section_break = False
            elif has_picture and len(segments) == 1:
                result.append(Para("", "picture"))
                after_section_break = False
        ppr = p_el.find(qn("w:pPr"))
        sect = ppr.find(qn("w:sectPr")) if ppr is not None else None
        if sect is not None:
            kind = following_type.get(id(sect), "nextPage")
            if by_marks:
                # Word's mark at the next page's start ends this one; only the side matters.
                if kind in ("oddPage", "evenPage"):
                    parity_needed = kind
            elif kind not in ("continuous", "nextColumn"):
                end_page()  # a next-page section break starts a new page
                after_section_break = True

    def table(tbl_el) -> None:
        nonlocal after_section_break
        after_section_break = False
        for row in tbl_el.findall(qn("w:tr")):
            cells, new_page = [], False
            for tc in row.findall(qn("w:tc")):
                parts = []
                for p in tc.iter(W_P):  # a cell's paragraphs, a space between them
                    for kind, value in walk(p):
                        if kind == "text":
                            parts.append(value)
                        elif (kind == "mark") == by_marks and kind in ("mark", "page"):
                            new_page = True
                    parts.append(" ")
                text = re.sub(r"[ \u00a0\u2028]+", " ", "".join(parts)).strip()
                if text:
                    cells.append(text)
            if new_page and (result and result[-1] is not None):
                end_page()
            if cells:
                result.append(Para(number_notes(VERSE_JOINER.join(cells)), "table-verse" if len(cells) > 1 else "table-text"))

    def block(el) -> None:
        if el.tag == W_P:
            ppr = el.find(qn("w:pPr"))
            sid = ppr.find(qn("w:pStyle")) if ppr is not None else None
            style = _docx_style_name(document, sid.get(qn("w:val")) if sid is not None else None)
            paragraph(el, style)
        elif el.tag == W_TBL:
            table(el)
        elif el.tag == W_SDT:  # a content control around paragraphs (a Word TOC often is one)
            content = el.find(qn("w:sdtContent"))
            for child in (content if content is not None else []):
                block(child)

    for child in body.iterchildren():
        block(child)
    result.extend(Para(n, "footnote text") for n in pending_notes)
    return _finish(result)


_W_T_RE = re.compile(r"<w:t(?: [^>]*)?>([^<]*)</w:t>")
_DROPPED_XML_RE = re.compile(r"<mc:Fallback>.*?</mc:Fallback>|<w:del\b.*?</w:del>", re.S)


def _letters(text: str) -> int:
    return len(re.findall(r"[^\W\d_]", text))


def docx_layout(path: Path) -> dict:
    """Where a .docx's pages come from, and whether Word's saved layout covers the whole
    file. Word writes its page marks only for the pages it had laid out when it saved; one
    real book had them for its first 40% -- the rest of it would be one enormous page."""
    import zipfile

    xml = zipfile.ZipFile(path).read("word/document.xml").decode("utf8", "replace")
    body = _DROPPED_XML_RE.sub("", xml.split("<w:body>", 1)[-1])
    manual = body.count('w:type="page"')
    marks = body.count("lastRenderedPageBreak")
    by_marks = marks > 0 and manual * FLOWING_TEXT_RATIO < marks
    total = _letters("".join(_W_T_RE.findall(body)))
    last = body.rfind("lastRenderedPageBreak")
    after = _letters("".join(_W_T_RE.findall(body[last:]))) if last >= 0 else total
    # The book's own TOC comes last and holds no marks (Word writes none inside it).
    toc_tail = _letters("".join(_W_T_RE.findall("".join(
        re.findall(r'<w:p\b(?:(?!</w:p>).)*?w:val="(?:TOC|toc)[^"]*"(?:(?!</w:p>).)*</w:p>', body[last:], re.S)))))
    return {
        "pagesFrom": "word-layout" if by_marks else "page-breaks",
        "marks": marks,
        "pageBreaks": manual,
        "unlaidOut": round((after - toc_tail) / total, 3) if by_marks and total else 0.0,
    }


def text_coverage(path: Path, content: dict) -> tuple[int, int]:
    """(letters in the file, letters in the converted book): body and footnotes, not
    headers. Whatever the reader doesn't know how to read -- a text box, a field it skips
    -- shows up as a difference instead of silently disappearing."""
    import zipfile

    if path.suffix.lower() == ".docx":
        z = zipfile.ZipFile(path)
        xml = _DROPPED_XML_RE.sub("", z.read("word/document.xml").decode("utf8", "replace"))
        source = _letters("".join(_W_T_RE.findall(xml.split("<w:body>", 1)[-1])))
        if "word/footnotes.xml" in z.namelist():
            notes = z.read("word/footnotes.xml").decode("utf8", "replace")
            notes = re.sub(r'<w:footnote [^>]*w:type="[^"]*"[^>]*>.*?</w:footnote>', "", notes, flags=re.S)
            source += _letters("".join(_W_T_RE.findall(_DROPPED_XML_RE.sub("", notes))))
    else:
        import olefile

        ole = olefile.OleFileIO(str(path))
        wd = ole.openstream("WordDocument").read()
        flags = struct.unpack_from("<H", wd, 0x0A)[0]
        tbl = ole.openstream("1Table" if flags & 0x0200 else "0Table").read()
        ccp_text, ccp_ftn = struct.unpack_from("<II", wd, 0x4C)
        source = _letters(_clean_text(_story_text(wd, _pieces(wd, tbl), ccp_text + ccp_ftn)))
    converted = _letters(" ".join(b.get("text") or "" for p in content.get("pages", []) for b in p.get("blocks", [])))
    return source, converted


def _docx_style_name(document, style_id: str | None) -> str:
    try:
        style = document.styles.get_by_id(style_id, 1)  # 1: WD_STYLE_TYPE.PARAGRAPH
    except Exception:  # noqa: BLE001 -- an unknown id is the default style
        style = None
    return style.name if style is not None else ""


def _docx_footnotes(document, walk) -> dict[str, str]:
    """Footnote id -> its text (the reference mark inside it kept as _NOTE), from the
    footnotes part. The separator entries (ids -1 and 0) are Word's own lines, not notes."""
    from docx.oxml.ns import qn
    from lxml import etree

    part = next((rel.target_part for rel in document.part.rels.values()
                 if rel.reltype.endswith("/footnotes")), None)
    if part is None:
        return {}
    root = etree.fromstring(part.blob)
    notes = {}
    for fn in root.iter(qn("w:footnote")):
        if fn.get(qn("w:type")) in ("separator", "continuationSeparator", "continuationNotice"):
            continue
        paras = []
        for p in fn.iter(qn("w:p")):
            text = "".join(v for kind, v in walk(p) if kind == "text")
            text = re.sub(r"[ \u00a0\u2028]+", " ", text).strip()
            if text:
                paras.append(text)
        notes[fn.get(qn("w:id"))] = " ".join(paras)
    return notes


def _footnotes_restart_each_page(document) -> bool:
    from docx.oxml.ns import qn

    settings = document.settings.element
    for el in [*settings.iter(qn("w:footnotePr")), *document.element.body.iter(qn("w:footnotePr"))]:
        restart = el.find(qn("w:numRestart"))
        if restart is not None:
            return restart.get(qn("w:val")) == "eachPage"
    return False


_ARABIC_ALPHA = "أبتثجحخدذرزسشصضطظعغفقكلمنهوي"
_ARABIC_ABJAD = "أبجدهوزحطيكلمنسعفصقرشتثخذضظغ"


class _DocxNumbering:
    """The label Word generates for a numbered paragraph ("1-", "أ)", "•"): list numbers are
    not text in the file, and without them "1- ... 2- ..." read as run-on sentences."""

    def __init__(self, document):
        from docx.oxml.ns import qn

        self.qn = qn
        self.document = document
        self.levels: dict[str, dict[int, tuple[str, str, int]]] = {}
        self.abstract_of: dict[str, str] = {}
        self.counters: dict[str, list[int]] = {}
        try:
            root = document.part.numbering_part.element
        except Exception:  # noqa: BLE001 -- no numbering part: nothing is numbered
            return
        abstract = {}
        for an in root.iter(qn("w:abstractNum")):
            lv = {}
            for lvl in an.iter(qn("w:lvl")):
                fmt = lvl.find(qn("w:numFmt"))
                text = lvl.find(qn("w:lvlText"))
                start = lvl.find(qn("w:start"))
                lv[int(lvl.get(qn("w:ilvl")))] = (
                    fmt.get(qn("w:val")) if fmt is not None else "decimal",
                    text.get(qn("w:val")) if text is not None else "",
                    int(start.get(qn("w:val"))) if start is not None else 1,
                )
            abstract[an.get(qn("w:abstractNumId"))] = lv
        for num in root.iter(qn("w:num")):
            ref = num.find(qn("w:abstractNumId"))
            if ref is not None and ref.get(qn("w:val")) in abstract:
                self.levels[num.get(qn("w:numId"))] = abstract[ref.get(qn("w:val"))]

    def _num_pr(self, p_el):
        qn = self.qn
        ppr = p_el.find(qn("w:pPr"))
        num_pr = ppr.find(qn("w:numPr")) if ppr is not None else None
        if num_pr is None and ppr is not None and ppr.find(qn("w:pStyle")) is not None:
            try:
                style = self.document.styles.get_by_id(ppr.find(qn("w:pStyle")).get(qn("w:val")), 1)
                sppr = style.element.find(qn("w:pPr")) if style is not None else None
                num_pr = sppr.find(qn("w:numPr")) if sppr is not None else None
            except Exception:  # noqa: BLE001
                num_pr = None
        if num_pr is None:
            return None
        num_id = num_pr.find(qn("w:numId"))
        ilvl = num_pr.find(qn("w:ilvl"))
        return (num_id.get(qn("w:val")) if num_id is not None else None,
                int(ilvl.get(qn("w:val"))) if ilvl is not None else 0)

    def label(self, p_el) -> str:
        pr = self._num_pr(p_el)
        if not pr or pr[0] in (None, "0") or pr[0] not in self.levels:
            return ""
        num_id, ilvl = pr
        levels = self.levels[num_id]
        if ilvl not in levels:
            return ""
        counters = self.counters.setdefault(num_id, [0] * 10)
        counters[ilvl] = counters[ilvl] + 1 if counters[ilvl] else levels[ilvl][2]
        for deeper in range(ilvl + 1, 10):
            counters[deeper] = 0
        fmt, text, _ = levels[ilvl]
        if fmt == "bullet":
            return "•"
        if fmt == "none":
            return ""

        def render(m):
            k = int(m.group(1)) - 1
            n = counters[k] or (levels.get(k, ("decimal", "", 1))[2])
            f = levels.get(k, (fmt,))[0]
            if f == "arabicAlpha":
                return _ARABIC_ALPHA[(n - 1) % len(_ARABIC_ALPHA)]
            if f == "arabicAbjad":
                return _ARABIC_ABJAD[(n - 1) % len(_ARABIC_ABJAD)]
            return str(n)
        return re.sub(r"%(\d)", render, text).strip()


def read_any(path: Path) -> list[Para | None]:
    return read_docx(path) if path.suffix.lower() == ".docx" else read_doc(path)


# ── Recover printed page numbers + headings from the book's own table of contents ──

_TOC_LINE_RE = re.compile(r"^\(?(.*?)\s+(\d{1,4})$")
# A section's line gives its page range, "البابُ الأوّل ... 17 ـ 127": it starts on the first.
_TOC_RANGE_RE = re.compile(r"^\(?(.*?)\s+(\d{1,4})\s*[ـ\-–]\s*\d{1,4}$")
_LEAD_NUM_RE = re.compile(r"^[\s(\[]*\d+\s*[ـ\-–.)]\s*")


def _key(text: str) -> str:
    from app.services.arabic import normalize

    text = text.replace("\u2028", " ")
    text = re.sub(r"\(\s*\d+\s*\)", " ", text)  # footnote markers such as "(4)"
    return re.sub(r"\s+", " ", normalize(_LEAD_NUM_RE.sub("", text))).strip("( [")


MIN_PROBE = 6  # letters an index title needs before it is searched for in the body


def anchor_toc(pages: list[list[Para]]):
    """Use the book's own TOC (paragraphs styled "TOC n" ending in a printed page number)
    to find, for each entry, the body paragraph it points at. Returns (anchors, toc_pages)
    where anchors is a list of (sequence, printed_number) in reading order. Matched
    paragraphs are flagged as headings, and a title the TOC wrapped over two lines is
    merged back into one heading."""
    toc_pages = [k for k, ps in enumerate(pages, 1) if any(p.style.upper().startswith("TOC") for p in ps)]
    if not toc_pages:
        return [], [], []
    toc_set = set(toc_pages)
    entries = []
    pending: list[str] = []  # unnumbered first lines of a wrapped TOC 2 title
    for k in toc_pages:
        for p in pages[k - 1]:
            if p.style.upper().startswith("TOC"):
                text = p.text.replace("\u2028", " ").strip()
                if p.style.upper().startswith("TOC 1"):
                    pending = []
                    continue
                m = _TOC_RANGE_RE.match(text) or _TOC_LINE_RE.match(text)
                if m and _key(m.group(1)):
                    title = " ".join([*pending, m.group(1).strip()])
                    # Matched by the title's beginning: the numbered line of a wrapped
                    # title can be as little as a symbol ("9 159" -- the font's honorific),
                    # which starts any number of unrelated body sentences.
                    entries.append((_key(title), int(m.group(2)), title))
                    pending = []
                elif text:
                    pending.append(text)
    entries = _contents_run(entries)

    anchors: list[tuple[int, int]] = []
    matched: dict[tuple[int, int], Para] = {}  # anchor -> the paragraph made a heading for it
    last_page, last_idx = 1, -1
    prev_match: Para | None = None
    unmatched: list[tuple[str, int, str]] = []
    for key, number, title in entries:
        probe = key[:22]
        found = None
        if len(probe) < MIN_PROBE:  # too little text to tell one paragraph from another
            unmatched.append((probe, number, title))
            continue
        for k in range(last_page, len(pages) + 1):
            if k in toc_set:
                continue
            start = last_idx + 1 if k == last_page else 0
            for i in range(start, len(pages[k - 1])):
                p = pages[k - 1][i]
                if is_footnote_style(p.style) or SEPARATOR_RE.match(p.text):
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
        matched.setdefault((k, number), p)
        last_page, last_idx = k, i
        # A title the TOC wrapped over two lines matches two adjacent body paragraphs.
        if prev_match is not None and pages[k - 1] and i > 0 and pages[k - 1][i - 1] is prev_match:
            prev_match.text = f"{prev_match.text} {p.text}"
            p.text = ""
        else:
            p.heading = True
            prev_match = p
    anchors = _consistent_anchors(pages, _drop_outlier_anchors(anchors))
    leftovers, soft = _fuzzy_headings(pages, anchors, unmatched)
    for k, number, p in soft:
        matched.setdefault((k, number), p)
    anchors = _consistent_anchors(pages, _drop_outlier_anchors(sorted({*anchors, *((k, n) for k, n, _ in soft)})))
    # A rejected match that is plain text far from where its number points was the wrong
    # paragraph -- a sentence that happens to start like the entry -- and is no heading. A
    # heading-styled one, or one near its page, is the right heading with a number that
    # doesn't fit (an index from another printing): it stays a heading.
    kept = {id(matched[a]) for a in anchors if a in matched}
    by_number = sorted((num, seq - num) for seq, num in anchors)
    for (seq, number), p in matched.items():
        if (seq, number) in anchors or id(p) in kept or not p.text or _HEADING_LIKE_RE.match(p.style):
            continue
        before = [off for num, off in by_number if num <= number]
        offset = before[-1] if before else (by_number[0][1] if by_number else 0)
        if abs(seq - (number + offset)) > WRONG_MATCH_DISTANCE:
            p.heading = False
    _complete_chapter_titles(pages, toc_pages)
    recovered = _recover_missing(pages, anchors, leftovers, toc_set)
    return anchors, toc_pages, recovered


# A drop in page number bigger than this between two index lines starts a new block.
TOC_RESTART = 20
TOC_MIN_BLOCK = 5  # lines in order it takes for a block to be (part of) a table of contents


def _contents_run(entries):
    """The table of contents among everything styled TOC. A book can also style its
    alphabetical indexes (hadith, verses, names, sources) as TOC lines -- thousands of
    them in one real book -- and keep a short summary of the contents at the front. The
    contents runs through the book in order, so its numbers rise; an index's jump about.
    Cut the lines where the number falls back sharply, call a block of TOC_MIN_BLOCK or
    more lines ordered, and keep the stretch of ordered blocks that spans most of the book
    (a lone short block between two ordered ones is a misprint inside the contents, not an
    index). Span, not length: an index of poems listed by page rises too, but covers only
    the pages with poems, while the contents runs from the first page to the last."""
    if len(entries) < TOC_MIN_BLOCK:
        return entries
    blocks, cur = [], [entries[0]]
    for e in entries[1:]:
        if e[1] < cur[-1][1] - TOC_RESTART:
            blocks.append(cur)
            cur = []
        cur.append(e)
    blocks.append(cur)
    ordered = [len(b) >= TOC_MIN_BLOCK for b in blocks]
    best, best_len, i = None, (0, 0), 0
    while i < len(blocks):
        if not ordered[i]:
            i += 1
            continue
        # A misprint falls back inside the pages already covered (122, then 65, in a run
        # from 2); a new list starts below all of them (an index of poems by page, 274 ...
        # 350, then the contents from 2) and is not joined on.
        j, low = i, blocks[i][0][1]

        def joins(k):
            return k < len(blocks) and ordered[k] and blocks[k][0][1] >= low

        while j + 1 < len(blocks) and (joins(j + 1) or (not ordered[j + 1] and joins(j + 2))):
            j += 1
            low = min(low, min(e[1] for e in blocks[j]))
        while not ordered[j]:
            j -= 1
        numbers = [e[1] for b in blocks[i:j + 1] for e in b]
        size = (max(numbers) - min(numbers), len(numbers))
        if size > best_len:
            best, best_len = (i, j), size
        i = j + 1
    if best is None:
        return entries
    run = [e for b in blocks[best[0]:best[1] + 1] for e in b]
    while len(run) > 1 and run[0][1] > run[1][1]:  # the last line of an index just before it
        run.pop(0)
    return run


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
    toc_set = set(toc_pages)
    for k in range(1, len(pages) + 1):
        if k in toc_set:
            continue
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
    soft: list[tuple[int, int, Para]] = []  # (position, printed number, paragraph) from these matches
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
            soft.append((target_k, number, target))
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
            soft.append((target_k, number, target))
        else:
            leftovers.append((probe, number, title, expected))
    return leftovers, soft


def _words(text: str) -> set[str]:
    return {w for w in _key(text).split() if len(w) >= 3}


def _recover_missing(pages, anchors, leftovers, toc_set):
    """Index entries with no heading paragraph of their own. Each is one of:
    a continuation line of a title the index wrapped (merged into that heading), a title
    glued into another paragraph after a soft line break (that paragraph is split so the
    heading lands where it really is), or absent from the body (a heading with the
    index's title is inserted at the top of the page the number points to). Returns
    (printed number, title, how) for each, so a caller can report them."""
    from difflib import SequenceMatcher

    report = []
    for probe, number, title, expected in leftovers:
        lo, hi = max(1, expected - 1), min(len(pages), expected + 2)
        done = False
        # (a) continuation of a wrapped title already present as a heading
        for k in range(lo, hi + 1):
            if k in toc_set:
                continue
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
            if k in toc_set:
                continue
            ps = pages[k - 1]
            for i, q in enumerate(ps):
                if q.heading or "\u2028" not in q.text or is_footnote_style(q.style):
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
        # Only where the number points into the body: clamping a number the file doesn't
        # have to its first or last page stacked thousands of titles onto one page.
        if 1 <= expected <= len(pages) and expected not in toc_set:
            pages[expected - 1].insert(0, Para(title, "toc-inserted", heading=True))
            report.append((number, title, f"inserted at the top of page {expected}"))
        else:
            report.append((number, title, "not placed: its page is not in the body"))
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


# How far (in pages) from where its number points a rejected plain-text match must be to be
# taken for the wrong paragraph. Real headings under an index a few dozen pages off (another
# printing) stay headings; the sentence that caused the 557 -> 159 jump was 400 pages away.
WRONG_MATCH_DISTANCE = 50

# How many printed numbers an index may jump ahead of the file between two entries: a
# page or two the file lacks (a blank verso Word needed no break for). More is a wrong match.
MAX_NUMBER_SKIP = 2


def _consistent_anchors(pages: list[list[Para]], anchors: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The largest set of anchors that can all be true at once. Going through the book,
    position and printed number both move forward; the file can run ahead of the numbers
    only by empty pages it has in between (the break artifacts `artifact_pages` drops),
    and the numbers can run ahead of the file only by MAX_NUMBER_SKIP. One index line
    matching a like-worded sentence hundreds of pages away numbered a real book's last
    30 pages 159, 160, ... after 557 -- the outlier check can't see a wrong match at
    the end, with no neighbour after it."""
    if len(anchors) < 2:
        return anchors
    empty_before = [0]
    for pg in pages:
        empty_before.append(empty_before[-1] + (not pg))

    def fits(a, b):
        (sa, na), (sb, nb) = a, b
        if sb < sa or nb < na or (sb == sa and nb != na):
            return False
        drift = (sb - nb) - (sa - na)
        between = empty_before[sb - 1] - empty_before[sa] if sb > sa else 0
        return -MAX_NUMBER_SKIP <= drift <= between

    best = [1] * len(anchors)
    prev = [-1] * len(anchors)
    for j in range(len(anchors)):
        for i in range(j):
            if best[i] + 1 > best[j] and fits(anchors[i], anchors[j]):
                best[j], prev[j] = best[i] + 1, i
    j = max(range(len(anchors)), key=lambda k: (best[k], -k))
    chain = []
    while j != -1:
        chain.append(anchors[j])
        j = prev[j]
    return chain[::-1]


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
                      if ps[i].text and not is_footnote_style(ps[i].style)), None)
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


def _verse_lines(p: Para) -> list[str]:
    """A poem keeps its lines: one block per line as Word shows it (the library's verses are
    one block each). A poem is a poem-styled paragraph, or a centred one broken into three or
    more lines; any other soft line break is just layout and becomes a space."""
    lines = [re.sub(r"[ \u00a0]+", " ", x).strip() for x in _strip_angles(p.text).split("\u2028")]
    lines = [x for x in lines if x]
    poem = is_poem_style(p.style) or ("center" in p.style.lower() and len(lines) >= 3)
    if poem and len(lines) > 1:
        return lines
    return [" ".join(lines)] if lines else []


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
            if in_footnotes or is_footnote_style(p.style):
                lines += [f"< هامش > {line} < / هامش >" for line in _verse_lines(p)]
            elif p.heading or heading_re.match(p.style):
                lines += ["< فهرس الموضوعات >", text, "< / فهرس الموضوعات >"]
            else:
                lines += _verse_lines(p)
    lines.append("< / الكتاب >")
    return "\n".join(lines)


def _toc_numbers(pages: list[list[Para]]) -> dict[str, int]:
    """Index title (normalised) -> the page number the book's own TOC gives it."""
    out = {}
    for ps in pages:
        for p in ps:
            if p.style.upper().startswith("TOC"):
                text = p.text.replace("\u2028", " ").strip()
                m = _TOC_RANGE_RE.match(text) or _TOC_LINE_RE.match(text)
                if m and _key(m.group(1)):
                    out.setdefault(_key(m.group(1))[:22], int(m.group(2)))
    return out


def _split_toc_pages(pages: list[list[Para]]) -> list[list[Para]]:
    """Word writes no page marks inside its own TOC field, so a TOC several pages long read
    by Word's marks arrives as one page (a real book: 173 lines, printed on 7 pages, on one;
    the book came out 6 pages short). The TOC gives its own page and the next heading's,
    which says how many pages it takes; its lines are all one height, so they are shared
    out evenly."""
    numbers = _toc_numbers(pages)
    if not numbers:
        return pages

    def heading_number(ps):
        for p in ps:
            if p.text and not p.style.upper().startswith("TOC") and HEADING_STYLE_RE.match(p.style):
                return numbers.get(_key(p.text)[:22])
        return None

    out: list[list[Para]] = []
    k = 0
    while k < len(pages):
        ps = pages[k]
        toc_lines = [p for p in ps if p.style.upper().startswith("TOC")]
        own = heading_number(ps)
        nxt = next(((j, heading_number(pages[j])) for j in range(k + 1, min(len(pages), k + 6))
                    if heading_number(pages[j]) is not None), None)
        if len(toc_lines) >= 20 and own is not None and nxt is not None:
            j, following = nxt
            extra = (following - own) - (j - k)
            if 0 < extra <= len(toc_lines) // 5:
                n = extra + 1
                head = [p for p in ps if p not in toc_lines]
                size = -(-len(toc_lines) // n)
                chunks = [toc_lines[i:i + size] for i in range(0, len(toc_lines), size)]
                out.append(head + chunks[0])
                out.extend(chunks[1:])
                out.extend([] for _ in range(n - len(chunks)))
                k += 1
                continue
        out.append(ps)
        k += 1
    return out


def convert_doc(path, title, author, front_pages, first_printed, heading_re, book_id="900001",
                use_toc=True, extra_metadata=None, blank_pages=()):
    items = read_any(path)
    pages = _split_toc_pages(_split_pages(items))
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
