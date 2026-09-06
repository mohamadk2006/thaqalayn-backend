#!/usr/bin/env python3
"""Convert a Shamela `.abx` export into the v2 BookContent JSON structure.

Target structure, per the spec this implements exactly:

    Book -> Pages -> ordered Blocks (text | heading | footnotes), plus a flat toc[].

Deliberately NOT Book -> Chapter -> Section -> Paragraph: ABX does not reliably encode
semantic chapters/sections/paragraphs, only page markers, TOC headings, and footnote
blocks. This conversion stays as close to lossless as possible -- no paragraph
splitting, no heading hierarchy, no footnote-by-footnote parsing, no multi-work
detection. Those are all explicitly deferred to a later, separate processing stage.

STANDALONE MODULE -- not yet wired into the production import pipeline
(scripts/import/import_books.py still uses the v1 chapters/sections/paragraphs shape,
which is what's actually deployed and what the iOS app currently parses). This exists
to validate the new structure against real sources and produce real output samples
before any cutover, since switching the live pipeline over is a separate, coordinated
step -- it needs the app updated to parse this shape, the importer/paging logic
rewritten, and a new Postgres schema, none of which this file touches.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import deque
from pathlib import Path

METADATA_TAG_RE = re.compile(r"^<\s*(?P<tag>[^=>]+?)\s*>\s*(?P<value>.*?)\s*<\s*/\s*(?P=tag)\s*>$")
HEADING_OPEN = "< فهرس الموضوعات >"
HEADING_CLOSE = "< / فهرس الموضوعات >"
FOOTNOTE_OPEN = "< هامش >"
FOOTNOTE_CLOSE = "< / هامش >"
# Unanchored counterparts of the four tag pairs above -- real lines routinely carry body
# prose both before AND after a tag (a footnote reference mid-sentence: '...قال ( 1 )
# < هامش > نص الهامش < / هامش > . وتابع ...'). The anchored ^...$ forms above only ever
# matched a tag that filled its *entire* line, which is common but far from universal:
# checked against the full 18,831-file corpus, a naive line-by-line scan using only the
# anchored forms mislabels or loses real content in the large majority of files, because
# the code fell through to treating the whole line (tag characters included) as a single
# opaque text block, or -- worse -- because the old multi-line block reader required its
# closing tag to be the last thing on the line, so any trailing punctuation after
# '< / هامش >' (extremely common: a period, a semicolon) made it miss the real close and
# keep consuming every following line -- including unrelated main-text paragraphs -- as
# 'footnote' content until it stumbled on some later line that happened to end exactly on
# the close tag, or hit EOF. parse_body finds and resolves these tags by leftmost
# position within a line instead, splitting off and requeueing whatever text surrounds
# them, so multiple tags per line and tags with prose on either side both resolve
# correctly.
PAGE_PAIR_RE = re.compile(r"<\s*صفحة\s*>\s*(?P<label>.*?)\s*<\s*/\s*صفحة\s*>")
BLANK_PAGE_PAIR_RE = re.compile(r"<\s*صفحة\s+فارغة\s*>\s*(?P<label>.*?)\s*<\s*/\s*صفحة\s+فارغة\s*>")
HEADING_PAIR_RE = re.compile(r"<\s*فهرس\s+الموضوعات\s*>\s*(?P<text>.*?)\s*<\s*/\s*فهرس\s+الموضوعات\s*>")
FOOTNOTE_PAIR_RE = re.compile(r"<\s*هامش\s*>\s*(?P<text>.*?)\s*<\s*/\s*هامش\s*>")
_PAIR_KINDS = (
    ("blank_page", BLANK_PAGE_PAIR_RE),
    ("page", PAGE_PAIR_RE),
    ("heading", HEADING_PAIR_RE),
    ("footnote", FOOTNOTE_PAIR_RE),
)
BLANK_PAGE_OPEN = "< صفحة فارغة >"
BLANK_PAGE_CLOSE = "< / صفحة فارغة >"
_BARE_OPEN_KINDS = (
    ("heading_open", HEADING_OPEN),
    ("footnote_open", FOOTNOTE_OPEN),
    ("blank_page_open", BLANK_PAGE_OPEN),
)
BODY_MARKER = "< الكتاب >"
# Any OTHER complete `< tag >` or `< / tag >` -- Shamela sources use a long tail of
# sectioning/formatting tags beyond the four with real schema meaning above: poetry
# (< شعر >), commentary (< شرح >), attachments (< ملحق = N >), inline language switches
# (< لغة النص = انجليزي >, seen used per-word inside dictionary entries), glossary terms,
# Q&A markers, and others. None of them carry a distinct field in the v2 schema, and --
# critically -- their *content* is ordinary flowing text that itself may contain a real
# page break, heading, or footnote (observed nested inside both < ملحق > and < شرح > in
# real files), so treating them as opaque "read until the matching close" blocks like
# heading/footnote would swallow those nested real tags as literal text. Instead each
# complete tag of this kind is deleted in place, wherever it falls in a line, and
# everything else keeps flowing through the same per-line scan uninterrupted -- this also
# subsumes the old whole-line-only "unrecognized directive" skip (e.g.
# < لغة النص = عربي > alone on its own line) as the same mechanism.
GENERIC_TAG_RE = re.compile(r"<\s*/?\s*[^<>]+?\s*>")
LEADING_DIGITS_RE = re.compile(r"^\s*(\d+)")
TRAILING_DIGITS_RE = re.compile(r"(\d+)\s*$")

# Same cross-reference markup v1 strips -- irrelevant to the structural rewrite, but a
# raw < ارتباط > tag left in would otherwise be misread as an unrecognized directive.
QURAN_LINK_TAG_RE = re.compile(r" ?<\s*/?\s*ارتباط\s*=\s*\d+\s*> ?")

METADATA_KEYS = {
    "سنة الوفاة": "authorDeath",
    "مجموعة": "collection",
    "جزء": "volume",
    "تحقيق": "editor",
    "طبعة": "edition",
    "سنة الطبع": "publicationYear",
    "مطبعة": "printer",
    "الناشر": "publisher",
    "ردمك": "isbn",
    "ملاحظات الهوية": "notes",
    "ملف مرفق": "attachedFile",
    "تطابق الصفحات": "pageMatched",
    "التاريخ": "sourceDate",
    "موثوق": "trusted",
}
_BOOLEAN_KEYS = {"pageMatched", "trusted"}


class ConversionError(Exception):
    """Raised for a file that cannot be parsed. Carries a human-readable reason so the
    batch driver can log it and move on rather than aborting the whole run."""


def _decode(path: Path) -> list[str]:
    for encoding in ("utf-8", "utf-8-sig", "cp1256"):
        try:
            text = path.read_text(encoding=encoding)
            return QURAN_LINK_TAG_RE.sub(" ", text).splitlines()
        except UnicodeDecodeError:
            continue
    raise ConversionError("undecodable text (tried utf-8, utf-8-sig, cp1256)")


def parse_metadata(lines: list[str]) -> tuple[dict[str, str], int]:
    """Read tagged metadata from line 1 (line 0 is a checksum header) until the body
    marker. Returns the raw tag->value dict and the index of the body's first line."""
    metadata: dict[str, str] = {}
    for index in range(1, len(lines)):
        line = lines[index].strip()
        if line == BODY_MARKER:
            return metadata, index + 1
        if match := METADATA_TAG_RE.match(line):
            metadata[match.group("tag")] = match.group("value")
    raise ConversionError(f"no body marker '{BODY_MARKER}' found")


def _build_metadata(raw: dict[str, str]) -> dict:
    """Map raw Shamela tags to BookMetadata fields. Per rule 24: an empty/missing field
    is omitted entirely, never invented as an empty string or a guessed value."""
    out: dict = {}
    for tag, key in METADATA_KEYS.items():
        value = raw.get(tag, "").strip()
        if not value:
            continue
        if key in _BOOLEAN_KEYS:
            out[key] = value == "1"
        else:
            out[key] = value
    return out


def _printed_number(label: str) -> int | None:
    """Extract the actual printed page number from a page label.

    Prefers a LEADING digit run: some sources (the Qur'an's own text, at least) label
    pages 'N ( سورة الفاتحة 1 )' -- printed page N followed by a surah/ayah reference in
    parentheses, so the real page number comes first and the trailing digit is an ayah
    number instead, not a page. Front-matter labels are the opposite shape ('تعريف
    الكتاب 1', 'كلمة المشرف 14') -- text first, page number trailing -- so a label with
    no leading digit falls back to the trailing digit run. A bare '25' matches either
    way as 25."""
    if match := LEADING_DIGITS_RE.match(label):
        return int(match.group(1))
    if match := TRAILING_DIGITS_RE.search(label):
        return int(match.group(1))
    return None


def _strip_generic_tags(text: str) -> str:
    """Deletes any complete generic (non-page/heading/footnote/blank) tag from `text`,
    same as the main loop's 'generic_tag' handling -- needed here too because a
    multi-line heading/footnote/blank-page span's interior lines never pass back through
    _find_first_tag (only the line containing the real close tag gets inspected, for
    close_tag itself), so a real citation like '< لغة النص = انجليزي > ... < / لغة النص
    = انجليزي >' sitting inside a long footnote -- extremely common, English book/author
    names cited mid-footnote -- would otherwise survive untouched in the block's text."""
    while match := GENERIC_TAG_RE.search(text):
        text = f"{text[:match.start()]} {text[match.end():]}"
    return " ".join(text.split())


def _read_block(first_fragment: str, queue: deque[str], close_tag: str) -> tuple[str, str]:
    """Reads a heading/footnote/blank-page block whose close tag wasn't found on its
    opening line. `first_fragment` is whatever followed the open tag on that same line.
    Pulls further lines from `queue` until one *contains* close_tag (not merely ends
    with it -- real closes are routinely followed by trailing punctuation, e.g.
    '. < / هامش > .'), then returns (block_text, leftover) where leftover is whatever
    trailed the close tag on that line, pushed back onto the caller's queue for normal
    reprocessing."""
    fragments: list[str] = []
    first_fragment = _strip_generic_tags(first_fragment)
    if first_fragment:
        fragments.append(first_fragment)
    while queue:
        candidate = queue.popleft().strip()
        index = candidate.find(close_tag)
        if index != -1:
            prefix = _strip_generic_tags(candidate[:index])
            if prefix:
                fragments.append(prefix)
            return "\n".join(fragments), candidate[index + len(close_tag):].strip()
        fragments.append(_strip_generic_tags(candidate))
    raise ConversionError(f"block never closed ({close_tag})")


def _find_first_tag(line: str):
    """Leftmost recognized tag in `line`: a fully-closed pair (page/blank/heading/
    footnote, wherever it sits amid surrounding prose), a bare open tag with no matching
    close on this line (heading/footnote/blank-page -- the only tags that legitimately
    span multiple lines), or any other single complete `< tag >`/`< / tag >` (deleted in
    place -- see GENERIC_TAG_RE). Returns (start, kind, match_or_pos) or None."""
    candidates: list[tuple[int, str, object]] = []
    for kind, regex in _PAIR_KINDS:
        m = regex.search(line)
        if m:
            candidates.append((m.start(), kind, m))
    for kind, open_tag in _BARE_OPEN_KINDS:
        pos = line.find(open_tag)
        if pos != -1:
            candidates.append((pos, kind, pos))
    m = GENERIC_TAG_RE.search(line)
    if m:
        candidates.append((m.start(), "generic_tag", m))
    if not candidates:
        return None
    # Stable sort: a pair match, a bare-open candidate, and the generic fallback can all
    # share the same start position (the same tag, matched three different ways) --
    # pair entries were appended first and bare-opens before the generic fallback, so
    # ties correctly prefer the most specific interpretation.
    candidates.sort(key=lambda c: c[0])
    return candidates[0]


_NUMERIC_WITH_SUFFIX_RE = re.compile(r"^\d+(\s*\(.*\))?$")


def _looks_numeric(label: str) -> bool:
    """A label counts as 'just a printed page number' for classification purposes even
    when it carries a parenthetical annotation after the number -- e.g. the Qur'an's own
    '1 ( الفاتحة 1 )' labels are genuine main-book pagination with a verse reference
    attached, not front-matter text like 'تعريف الكتاب 1'. Without this, a source whose
    every single page carries such an annotation (the Qur'an: all 6,350 of them) reads as
    'saw non-numeric labels, never found a reset' and gets misclassified as front matter
    from cover to cover."""
    return bool(_NUMERIC_WITH_SUFFIX_RE.match(label.strip()))


def _classify_split(labels: list[str]) -> int:
    """Decide how many of `labels`, from the start, are front matter -- returns the
    index of the first 'main' page (0 means the book has no front matter at all).

    Needs to see the *whole* label sequence before deciding, not just each one as it
    arrives: a page's own text ('تعريف الكتاب 1') proves front matter unambiguously, but
    a bare numeric label is inherently ambiguous on its own -- '33' opening a file could
    be page 33 of unlabeled front matter, or a later volume's main pagination picking up
    where the previous volume left off (seen in real sources: a volume with NO front
    matter, whose first printed page is already in the double digits). The only genuine
    evidence either way is what happens *later*: a printed-number reset (rule 10) proves
    everything before it was front matter, textually labeled or not; reaching the end of
    the file with the count still climbing and no reset proves the opposite -- there
    never was any front matter to transition out of, and it was 'main' all along.
    """
    highest = 0
    saw_text_label = False
    for i, label in enumerate(labels):
        printed = _printed_number(label)
        if not _looks_numeric(label):
            saw_text_label = True
        if printed is not None:
            if printed < highest and i > 0:
                return i
            highest = max(highest, printed)
    return len(labels) if saw_text_label else 0


def parse_body(lines: list[str], start: int) -> tuple[list[dict], list[dict]]:
    """Walk the body, producing (pages, toc) in the v2 shape."""
    pages: list[dict] = []
    labels: list[str] = []
    toc: list[dict] = []
    current_page: dict | None = None
    block_order = 0
    toc_order = 0

    def new_page(label: str) -> dict:
        nonlocal current_page, block_order
        printed = _printed_number(label)

        # The Qur'an (and anything else that tags every verse/paragraph with its own
        # < صفحة > marker) repeats the same printed number across many consecutive
        # markers -- one real Mushaf page, several verses, several markers. Those are
        # the same physical page, not one page each: continue it instead of starting a
        # new one whenever the printed number hasn't actually moved.
        if (
            current_page is not None
            and printed is not None
            and current_page.get("printedPage") == printed
        ):
            return current_page

        sequence = len(pages) + 1
        current_page = {
            "id": f"p-{sequence:06d}",
            "sequence": sequence,
            "isBlank": False,
            "blocks": [],
        }
        if printed is not None:
            current_page["printedPage"] = printed
        if label:
            current_page["sourcePageLabel"] = label
        pages.append(current_page)
        labels.append(label)
        block_order = 0
        return current_page

    def ensure_page() -> dict:
        """Body content appearing before any < صفحة > marker at all -- not seen in any
        real sample so far, but rule 28 (lossless, don't drop content) means it needs
        somewhere to land rather than raising."""
        if current_page is None:
            return new_page("")
        return current_page

    def add_text_block(text: str) -> None:
        nonlocal block_order
        if not text:
            return
        page = ensure_page()
        block_order += 1
        page["blocks"].append({
            "id": f"{page['id']}-b-{block_order:03d}",
            "type": "text",
            "order": block_order,
            "text": text,
        })

    def add_heading_block(text: str) -> None:
        nonlocal block_order, toc_order
        page = ensure_page()
        block_order += 1
        toc_order += 1
        toc_id = f"toc-{toc_order:05d}"
        page["blocks"].append({
            "id": f"{page['id']}-b-{block_order:03d}",
            "type": "heading",
            "order": block_order,
            "text": text,
            "tocId": toc_id,
        })
        # pageNumber isn't resolved yet -- frontMatter/main classification needs the
        # whole file's labels, filled in once parsing finishes (see below).
        toc.append({"id": toc_id, "order": toc_order, "title": text, "pageId": page["id"]})

    def add_footnote_block(text: str) -> None:
        nonlocal block_order
        page = ensure_page()
        block_order += 1
        page["blocks"].append({
            "id": f"{page['id']}-b-{block_order:03d}",
            "type": "footnotes",
            "order": block_order,
            "text": text,
        })

    def mark_blank(label: str) -> None:
        page = ensure_page()
        page["isBlank"] = True
        if label and not page.get("sourcePageLabel"):
            page["sourcePageLabel"] = label

    _BARE_OPEN_TAGS = {
        "heading_open": (HEADING_OPEN, HEADING_CLOSE),
        "footnote_open": (FOOTNOTE_OPEN, FOOTNOTE_CLOSE),
        "blank_page_open": (BLANK_PAGE_OPEN, BLANK_PAGE_CLOSE),
    }

    queue: deque[str] = deque(lines[start:])
    while queue:
        line = queue.popleft().strip()

        if not line or line == "< / الكتاب >":
            continue

        found = _find_first_tag(line)
        if found is None:
            add_text_block(line)
            continue

        tag_start, kind, payload = found
        before = line[:tag_start].strip()

        if kind in ("blank_page", "page", "heading", "footnote"):
            match = payload
            add_text_block(before)
            after = line[match.end():].strip()
            if kind == "page":
                new_page(match.group("label"))
            elif kind == "blank_page":
                mark_blank(match.group("label"))
            elif kind == "heading":
                add_heading_block(_strip_generic_tags(match.group("text")))
            elif kind == "footnote":
                add_footnote_block(_strip_generic_tags(match.group("text")))
            if after:
                queue.appendleft(after)
            continue

        if kind == "generic_tag":
            # A sectioning/formatting tag with no distinct schema meaning (poetry,
            # commentary, attachments, inline language switches, ...) -- deleted in
            # place rather than treated as a block boundary, since its content is
            # ordinary flowing text that may itself contain a real page/heading/
            # footnote tag later in the stream.
            add_text_block(before)
            after = line[payload.end():].strip()
            if after:
                queue.appendleft(after)
            continue

        # bare open with no close on this line -- reads forward across lines until the
        # close tag turns up, wherever in that later line it lands.
        add_text_block(before)
        open_tag, close_tag = _BARE_OPEN_TAGS[kind]
        first_fragment = line[tag_start + len(open_tag):]
        text, leftover = _read_block(first_fragment, queue, close_tag)
        if kind == "heading_open":
            add_heading_block(text)
        elif kind == "footnote_open":
            add_footnote_block(text)
        else:
            mark_blank(text)
        if leftover:
            queue.appendleft(leftover)

    split = _classify_split(labels)
    front_matter_index = 0
    for i, page in enumerate(pages):
        if i < split:
            page["pageType"] = "frontMatter"
            front_matter_index += 1
            page["pageNumber"] = f"0.{front_matter_index}"
        else:
            page["pageType"] = "main"
            printed = page.get("printedPage")
            page["pageNumber"] = str(printed) if printed is not None else str(page["sequence"])

    page_numbers = {page["id"]: page["pageNumber"] for page in pages}
    for entry in toc:
        entry["pageNumber"] = page_numbers[entry["pageId"]]

    return pages, toc


def convert(input_path: Path, book_id: str) -> dict:
    """Return the v2 BookContent dict. Raises ConversionError on any malformed input."""
    lines = _decode(input_path)
    raw_metadata, body_start = parse_metadata(lines)
    pages, toc = parse_body(lines, body_start)

    if not pages:
        raise ConversionError("no pages parsed from body")

    title = raw_metadata.get("اسم الكتاب", input_path.stem).strip()
    author = raw_metadata.get("اسم المؤلف", "").strip()

    return {
        # 2, not 1: this is the page/block/toc shape, not v1's chapters/sections/
        # paragraphs -- a reader that doesn't check this and assumes v1's layout would
        # silently misparse every field from here down.
        "schemaVersion": 2,
        "bookId": book_id,
        "title": title,
        "author": author,
        "metadata": _build_metadata(raw_metadata),
        "pages": pages,
        "toc": toc,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("input", type=Path, help="path to the .abx export")
    parser.add_argument("output", type=Path, help="path to write the v2 BookContent JSON")
    parser.add_argument("--book-id", help="bookId to embed (defaults to the input stem)")
    parser.add_argument("--pretty", action="store_true", help="indent output (debugging)")
    args = parser.parse_args()

    book_id = args.book_id or args.input.stem
    try:
        content = convert(args.input, book_id)
    except ConversionError as error:
        print(f"error: {args.input.name}: {error}", file=sys.stderr)
        return 1

    indent = 2 if args.pretty else None
    separators = None if args.pretty else (",", ":")
    args.output.write_text(
        json.dumps(content, ensure_ascii=False, indent=indent, separators=separators),
        encoding="utf-8",
    )
    print(
        f"{args.input.name}: {len(content['pages'])} pages, {len(content['toc'])} toc "
        f"entries -> {args.output}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
