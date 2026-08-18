#!/usr/bin/env python3
"""Survey a directory of Shamela .abx files without converting them.

Reads only each file's metadata header (everything before the `< الكتاب >` body marker),
so a full 18,000-file survey takes seconds rather than the hours a conversion would.

This is what establishes ground truth before any schema or taxonomy decision: the exact
set of `< مجموعة >` collection strings and how many books each holds, which metadata
fields are actually populated, how volumes distribute across works, and which files are
malformed. Guessing any of that from a handful of samples is how you end up re-importing
18,000 books.

Usage:
    python scripts/validate/scan_sources.py <directory> [--json report.json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

BODY_MARKER = "< الكتاب >"
TAG_RE = re.compile(r"^<\s*(?P<tag>[^>]+?)\s*>\s*(?P<value>.*?)\s*<\s*/\s*(?P=tag)\s*>$")

# Headers are read line by line in text mode rather than as a fixed byte slice. A fixed
# slice can split a multi-byte UTF-8 sequence at the boundary, which raises
# UnicodeDecodeError and — with a fallback encoding chain — silently yields mojibake for a
# perfectly valid file. That mis-diagnosed 41% of this library as corrupt on the first run.
# Text mode lets Python handle the buffering, so the boundary problem cannot occur.
MAX_HEADER_LINES = 200

ENCODINGS = ("utf-8", "utf-8-sig", "cp1256")


def read_metadata(path: Path) -> tuple[dict[str, str], str | None]:
    """Return (metadata, error). A malformed file yields ({}, reason) rather than raising —
    one bad book must never stop a survey of the whole library."""
    for encoding in ENCODINGS:
        try:
            with path.open(encoding=encoding) as handle:
                return _parse_header(handle)
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            return {}, f"unreadable: {exc}"
    return {}, f"undecodable text (tried {', '.join(ENCODINGS)})"


def _parse_header(handle) -> tuple[dict[str, str], str | None]:
    metadata: dict[str, str] = {}
    for index, line in enumerate(handle):
        if index == 0:
            continue  # line 0 is a checksum header, not content
        if index > MAX_HEADER_LINES:
            return metadata, f"no '{BODY_MARKER}' within the first {MAX_HEADER_LINES} lines"
        stripped = line.strip()
        if stripped == BODY_MARKER:
            if not metadata:
                return {}, "body marker found but no metadata tags"
            return metadata, None
        if match := TAG_RE.match(stripped):
            metadata[match.group("tag")] = match.group("value")
    return metadata, f"reached end of file without '{BODY_MARKER}'"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--json", type=Path, help="write the full report as JSON")
    args = parser.parse_args()

    paths = sorted(args.directory.glob("*.abx"))
    if not paths:
        print(f"error: no .abx files in {args.directory}", file=sys.stderr)
        return 1

    collections: Counter[str] = Counter()
    field_present: Counter[str] = Counter()
    all_fields: Counter[str] = Counter()
    languages: Counter[str] = Counter()
    works: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    errors: list[tuple[str, str]] = []
    volumes_seen = 0
    records: list[dict] = []

    for path in paths:
        metadata, error = read_metadata(path)
        if error:
            errors.append((path.name, error))
            continue

        for tag, value in metadata.items():
            all_fields[tag] += 1
            if value.strip():
                field_present[tag] += 1

        title = metadata.get("اسم الكتاب", "").strip()
        author = metadata.get("اسم المؤلف", "").strip()
        volume = metadata.get("جزء", "").strip()
        collection = metadata.get("مجموعة", "").strip() or "(empty)"

        collections[collection] += 1
        if volume:
            volumes_seen += 1
        works[(title, author)].append(volume or "-")
        languages["fa" if "فارسي" in title or "فارسى" in title else "ar"] += 1

        records.append(
            {
                "file": path.name,
                "title": title,
                "author": author,
                "volume": volume,
                "collection": collection,
                "death": metadata.get("سنة الوفاة", "").strip(),
                "publisher": metadata.get("الناشر", "").strip(),
            }
        )

    total = len(paths)
    ok = len(records)

    print(f"scanned {total} files — {ok} parsed, {len(errors)} failed\n")

    print(f"── collections (مجموعة): {len(collections)} distinct ──")
    for name, count in collections.most_common():
        print(f"  {count:6}  {name}")

    print(f"\n── metadata field fill rates (of {ok} books) ──")
    for tag, count in all_fields.most_common():
        filled = field_present[tag]
        print(f"  {filled:6} / {count:6}  ({100 * filled / ok:5.1f}%)  {tag}")

    multi = {k: v for k, v in works.items() if len(v) > 1}
    print("\n── works ──")
    print(f"  distinct (title, author) pairs : {len(works)}")
    print(f"  multi-volume works             : {len(multi)}")
    print(f"  files carrying a جزء number    : {volumes_seen} ({100 * volumes_seen / ok:.1f}%)")
    if multi:
        largest = sorted(multi.items(), key=lambda kv: -len(kv[1]))[:10]
        print("  largest works:")
        for (title, author), vols in largest:
            print(f"    {len(vols):4} vols  {title[:44]:44}  {author[:26]}")

    print("\n── language (heuristic: 'فارسي' in title) ──")
    for lang, count in languages.most_common():
        print(f"  {count:6}  {lang}")

    if errors:
        print(f"\n── failures ({len(errors)}) ──")
        for name, reason in errors[:20]:
            print(f"  {name}: {reason}")
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more")

    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "total": total,
                    "parsed": ok,
                    "collections": dict(collections),
                    "field_fill": {t: field_present[t] for t in all_fields},
                    "errors": [{"file": f, "reason": r} for f, r in errors],
                    "books": records,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"\nfull report → {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
