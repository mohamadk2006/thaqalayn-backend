#!/usr/bin/env python3
"""Convert a Shamela `.abx` export into the app's BookContent JSON schema.

Ported from the iOS project's Scripts/shamela_to_json.py, which is battle-tested on the
messy realities of the format (headings split across lines, closing tags trailing the
last fragment of title text). The parsing logic is preserved; three things changed for
the backend:

  1. The full metadata header is captured — not just title and author. The original
     converter discarded الناشر، مجموعة، سنة الوفاة and the rest; re-converting 18,798
     files later to recover them would be a waste. Everything is emitted as a sidecar
     manifest, keeping the book JSON at schemaVersion 1 so the iOS reader needs no change.
  2. Output is compact (no indentation). Pretty-printing 18,798 files is ~10 GB of pure
     whitespace for content nothing reads by hand.
  3. Volume is emitted as a real field in the manifest instead of being folded into the
     title string.

BookContent JSON (unchanged, consumed by the iOS reader):
    { schemaVersion, bookId, title, author, chapters: [ { id, title, order,
        sections: [ { id, title, order, paragraphs: [ { id, order, page, text } ] } ] } ] }

Manifest sidecar (backend-only, feeds the importer):
    { bookId, sourceFile, title, author, volume, metadata: {<raw Shamela tags>},
      pageFirst, pageLast, pageCount, paragraphCount, sectionCount }
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

METADATA_TAG_RE = re.compile(r"^<\s*(?P<tag>[^>]+?)\s*>\s*(?P<value>.*?)\s*<\s*/\s*(?P=tag)\s*>$")
PAGE_RE = re.compile(r"^<\s*صفحة\s*>\s*(?P<page>\d+)\s*<\s*/\s*صفحة\s*>$")
HEADING_OPEN = "< فهرس الموضوعات >"
HEADING_CLOSE = "< / فهرس الموضوعات >"
BODY_MARKER = "< الكتاب >"
BRACKETED_LINE_RE = re.compile(r"^<.*>$")

# The Shamela metadata tags worth carrying forward, with the manifest key each maps to.
# Tags outside this set are still captured verbatim under metadata["raw"], so nothing is
# lost, but these are the ones the importer promotes to real columns.
METADATA_KEYS = {
    "اسم الكتاب": "title",
    "اسم المؤلف": "author",
    "جزء": "volume",
    "سنة الوفاة": "death",
    "مجموعة": "collection",
    "الناشر": "publisher",
    "طبعة": "edition",
    "سنة الطبع": "published_year",
    "مطبعة": "printer",
    "تحقيق": "editor",
    "ردمك": "isbn",
    "ملف مرفق": "source_pdf",
    "ملاحظات الهوية": "identity_notes",
    "موثوق": "verified",
}


class ConversionError(Exception):
    """Raised for a file that cannot be parsed. Carries a human-readable reason so the
    batch driver can log it and move on rather than aborting the whole run."""


def _decode(path: Path) -> list[str]:
    """Shamela exports are UTF-8, but a few legacy files are cp1256. Try in order; never
    fall back to lossy decoding, which would silently corrupt Arabic text."""
    for encoding in ("utf-8", "utf-8-sig", "cp1256"):
        try:
            return path.read_text(encoding=encoding).splitlines()
        except UnicodeDecodeError:
            continue
    raise ConversionError("undecodable text (tried utf-8, utf-8-sig, cp1256)")


def parse_metadata(lines: list[str]) -> tuple[dict[str, str], int]:
    """Read tagged metadata from line 1 (line 0 is a checksum header) until the body
    marker. Returns the raw tag→value dict and the index of the body's first line."""
    metadata: dict[str, str] = {}
    for index in range(1, len(lines)):
        line = lines[index].strip()
        if line == BODY_MARKER:
            return metadata, index + 1
        if match := METADATA_TAG_RE.match(line):
            metadata[match.group("tag")] = match.group("value")
    raise ConversionError(f"no body marker '{BODY_MARKER}' found")


def parse_body(lines: list[str], start: int) -> list[dict]:
    """Build sections keyed off < فهرس الموضوعات > headings, each paragraph tagged with the
    most recent < صفحة > page number."""
    sections: list[dict] = []
    current_page = 1
    current_section: dict | None = None
    section_order = 0
    paragraph_order = 0

    def ensure_section(title: str) -> dict:
        nonlocal current_section, section_order, paragraph_order
        section_order += 1
        paragraph_order = 0
        current_section = {
            "id": f"sec-{section_order:03d}",
            "title": title,
            "order": section_order,
            "paragraphs": [],
        }
        sections.append(current_section)
        return current_section

    def read_heading(open_index: int) -> tuple[str, int]:
        """A heading opens on its own line but its title and closing tag may be split
        across several following lines (occasionally with the close tag trailing the last
        title fragment). Scan forward for the line ending with the close tag, joining
        every fragment seen."""
        fragments: list[str] = []
        index = open_index + 1
        while index < len(lines):
            candidate = lines[index].strip()
            if candidate.endswith(HEADING_CLOSE):
                prefix = candidate[: -len(HEADING_CLOSE)].strip()
                if prefix:
                    fragments.append(prefix)
                return " ".join(fragments), index + 1
            fragments.append(candidate)
            index += 1
        raise ConversionError(f"heading opened at line {open_index} never closed")

    index = start
    while index < len(lines):
        line = lines[index].strip()

        if not line or line == "< / الكتاب >":
            index += 1
            continue

        if page_match := PAGE_RE.match(line):
            current_page = int(page_match.group("page"))
            index += 1
            continue

        if line == HEADING_OPEN:
            title, index = read_heading(index)
            ensure_section(title)
            continue

        if BRACKETED_LINE_RE.match(line):
            index += 1  # an unrecognized directive (e.g. trailing language marker)
            continue

        if current_section is None:
            ensure_section("")  # front matter before the first heading

        paragraph_order += 1
        current_section["paragraphs"].append(
            {
                "id": f"{current_section['id']}-p-{paragraph_order:03d}",
                "order": paragraph_order,
                "page": current_page,
                "text": line,
            }
        )
        index += 1

    return sections


def convert(input_path: Path, book_id: str) -> tuple[dict, dict]:
    """Return (content, manifest). Raises ConversionError on any malformed input."""
    lines = _decode(input_path)
    raw_metadata, body_start = parse_metadata(lines)
    sections = parse_body(lines, body_start)

    if not sections:
        raise ConversionError("no sections parsed from body")

    title = raw_metadata.get("اسم الكتاب", input_path.stem).strip()
    author = raw_metadata.get("اسم المؤلف", "").strip()
    volume_raw = raw_metadata.get("جزء", "").strip()
    volume = int(volume_raw) if volume_raw.isdigit() else None

    # The first section holds front matter before the first real heading; if it never got
    # a title, use the book's own title as a natural intro heading. Note: unlike the iOS
    # converter, the volume is NOT folded into the title — it's a manifest field.
    if sections[0]["title"] == "":
        sections[0]["title"] = title

    content = {
        "schemaVersion": 1,
        "bookId": book_id,
        "title": title,
        "author": author,
        "chapters": [{"id": "ch-001", "title": title, "order": 1, "sections": sections}],
    }

    pages = [p["page"] for s in sections for p in s["paragraphs"]]
    paragraph_count = len(pages)
    promoted = {
        key: raw_metadata[tag].strip()
        for tag, key in METADATA_KEYS.items()
        if raw_metadata.get(tag, "").strip()
    }
    manifest = {
        "bookId": book_id,
        "sourceFile": input_path.name,
        "title": title,
        "author": author,
        "volume": volume,
        "metadata": promoted,
        "raw": raw_metadata,
        "pageFirst": min(pages) if pages else None,
        "pageLast": max(pages) if pages else None,
        "pageCount": len(set(pages)),
        "paragraphCount": paragraph_count,
        "sectionCount": len(sections),
    }
    return content, manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("input", type=Path, help="path to the .abx export")
    parser.add_argument("output", type=Path, help="path to write BookContent JSON")
    parser.add_argument("--manifest", type=Path, help="path to write the metadata sidecar")
    parser.add_argument("--book-id", help="bookId to embed (defaults to the input stem)")
    parser.add_argument(
        "--pretty", action="store_true", help="indent output (debugging; not for bulk runs)"
    )
    args = parser.parse_args()

    book_id = args.book_id or args.input.stem
    try:
        content, manifest = convert(args.input, book_id)
    except ConversionError as error:
        print(f"error: {args.input.name}: {error}", file=sys.stderr)
        return 1

    indent = 2 if args.pretty else None
    separators = None if args.pretty else (",", ":")
    args.output.write_text(
        json.dumps(content, ensure_ascii=False, indent=indent, separators=separators),
        encoding="utf-8",
    )
    if args.manifest:
        args.manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=indent, separators=separators),
            encoding="utf-8",
        )

    print(
        f"{args.input.name} → {args.output.name}: "
        f"{manifest['sectionCount']} sections, {manifest['paragraphCount']} paragraphs, "
        f"pages {manifest['pageFirst']}–{manifest['pageLast']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
