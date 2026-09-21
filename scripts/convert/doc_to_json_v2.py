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

Needs `olefile` (uv run --with olefile python scripts/convert/doc_to_json_v2.py ...).
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
    leftovers = _fuzzy_headings(pages, anchors, unmatched)
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
    for probe, number, title in unmatched:
        before = [off for num, off in by_number if num <= number]
        offset = before[-1] if before else (by_number[0][1] if by_number else 0)
        expected = number + offset
        best, target = 0.0, None
        for k in range(max(1, expected - 1), min(len(pages), expected + 2) + 1):
            for p in pages[k - 1]:
                if p.heading or not p.text or not _HEADING_LIKE_RE.match(p.style):
                    continue
                score = SequenceMatcher(None, probe, _key(p.text)[: len(probe)]).ratio()
                if score > best:
                    best, target = score, p
        if target is not None and best >= 0.7:
            target.heading = True
        else:
            leftovers.append((probe, number, title, expected))
    return leftovers


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


# ── Build the v2 book ─────────────────────────────────────────────────────────


def _split_pages(items: list[Para | None]) -> list[list[Para]]:
    pages: list[list[Para]] = [[]]
    for it in items:
        if it is None:
            pages.append([])
        else:
            pages[-1].append(it)
    return pages


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
                use_toc=True):
    items = read_doc(path)
    pages = _split_pages(items)
    anchors, recovered = [], []
    if use_toc:
        anchors, _, recovered = anchor_toc(pages)
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
    content["_anchors"] = anchors
    content["_recovered"] = recovered
    return content, items


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("doc", type=Path)
    ap.add_argument("--title", required=True)
    ap.add_argument("--author", default="")
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
            items = read_doc(args.doc)
            count = collections.Counter(p.style for p in items if p)
            samples: dict[str, str] = {}
            for p in items:
                if p and p.style not in samples:
                    samples[p.style] = p.text[:60]
            for style, n in count.most_common():
                print(f"{n:>6}  {style or '(none)':<30} e.g. {samples[style]}")
            return 0

        if args.heading_styles:
            wanted = {s.strip() for s in args.heading_styles.split(",")}
            heading_re = re.compile("^(" + "|".join(re.escape(s) for s in wanted) + ")$")
        else:
            heading_re = HEADING_STYLE_RE
        content, _ = convert_doc(
            args.doc, args.title, args.author, args.front_pages, args.first_printed,
            heading_re, args.book_id, use_toc=not args.no_toc,
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

    out = args.out or args.doc.with_suffix(".json")
    out.write_text(json.dumps(content, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"\nwrote {out}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
