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
from pathlib import Path

METADATA_TAG_RE = re.compile(r"^<\s*(?P<tag>[^=>]+?)\s*>\s*(?P<value>.*?)\s*<\s*/\s*(?P=tag)\s*>$")
PAGE_OPEN_RE = re.compile(r"^<\s*صفحة\s*>\s*(?P<label>.*?)\s*<\s*/\s*صفحة\s*>$")
BLANK_PAGE_RE = re.compile(r"^<\s*صفحة\s+فارغة\s*>.*<\s*/\s*صفحة\s+فارغة\s*>$")
HEADING_OPEN = "< فهرس الموضوعات >"
HEADING_CLOSE = "< / فهرس الموضوعات >"
FOOTNOTE_OPEN = "< هامش >"
FOOTNOTE_CLOSE = "< / هامش >"
# Both tags can also appear entirely on one line -- open, text, and close together --
# rather than opening on their own line with the content following. Real and common:
# 11,749 of 18,831 source files (62%) use this single-line form for at least one
# heading. Checked before the bare-open-tag multi-line case below.
HEADING_LINE_RE = re.compile(r"^< فهرس الموضوعات >\s*(?P<text>.*?)\s*< / فهرس الموضوعات >$")
FOOTNOTE_LINE_RE = re.compile(r"^< هامش >\s*(?P<text>.*?)\s*< / هامش >$")
BODY_MARKER = "< الكتاب >"
BRACKETED_LINE_RE = re.compile(r"^<.*>$")
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


def _read_block(lines: list[str], open_index: int, close_tag: str) -> tuple[str, int]:
    """Headings and footnotes both open on their own line and may span several more
    before the closing tag appears, occasionally trailing the last content fragment on
    the same line as the close tag. Shared with v1's heading reader, generalized to
    also read footnote blocks the same way."""
    fragments: list[str] = []
    index = open_index + 1
    while index < len(lines):
        candidate = lines[index].strip()
        if candidate.endswith(close_tag):
            prefix = candidate[: -len(close_tag)].strip()
            if prefix:
                fragments.append(prefix)
            return "\n".join(fragments), index + 1
        fragments.append(candidate)
        index += 1
    raise ConversionError(f"block opened at line {open_index} never closed ({close_tag})")


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
        sequence = len(pages) + 1
        current_page = {
            "id": f"p-{sequence:06d}",
            "sequence": sequence,
            "isBlank": False,
            "blocks": [],
        }
        printed = _printed_number(label)
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

    index = start
    while index < len(lines):
        line = lines[index].strip()

        if not line or line == "< / الكتاب >":
            index += 1
            continue

        if match := PAGE_OPEN_RE.match(line):
            new_page(match.group("label"))
            index += 1
            continue

        if BLANK_PAGE_RE.match(line):
            ensure_page()["isBlank"] = True
            index += 1
            continue

        heading_line = HEADING_LINE_RE.match(line)
        if heading_line or line == HEADING_OPEN:
            if heading_line:
                text, index = heading_line.group("text"), index + 1
            else:
                text, index = _read_block(lines, index, HEADING_CLOSE)
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
            toc.append({
                "id": toc_id,
                "order": toc_order,
                "title": text,
                "pageId": page["id"],
            })
            continue

        footnote_line = FOOTNOTE_LINE_RE.match(line)
        if footnote_line or line == FOOTNOTE_OPEN:
            if footnote_line:
                text, index = footnote_line.group("text"), index + 1
            else:
                text, index = _read_block(lines, index, FOOTNOTE_CLOSE)
            page = ensure_page()
            block_order += 1
            page["blocks"].append({
                "id": f"{page['id']}-b-{block_order:03d}",
                "type": "footnotes",
                "order": block_order,
                "text": text,
            })
            continue

        if BRACKETED_LINE_RE.match(line):
            index += 1  # an unrecognized directive (e.g. < لغة النص = عربي >)
            continue

        page = ensure_page()
        block_order += 1
        page["blocks"].append({
            "id": f"{page['id']}-b-{block_order:03d}",
            "type": "text",
            "order": block_order,
            "text": line,
        })
        index += 1

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
        "schemaVersion": 1,
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
