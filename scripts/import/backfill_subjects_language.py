#!/usr/bin/env python3
"""Backfill every book's content JSON with the corrected classification now in the DB.

Two fields, both under `metadata`:
  - `collection`: REPLACES the original raw `< مجموعة >` Shamela tag (~530 distinct,
    mostly noisy values -- see ShamelaCollection's docstring) with the actual, hand-
    verified multi-category classification from `work_subjects`, as an array of subject
    titles (a book can belong to more than one). `_lookup_collection` in import_books.py
    already tolerates this shape (returns None instead of raising), and the book upsert
    no longer overwrites `collection_id` on reimport for exactly this reason -- see the
    comment on that ON CONFLICT clause.
  - `language`: new field, not previously present in the v2 JSON at all. Carries the
    corrected ar/fa classification from `books.language_code` (the language-fix pass
    applied directly to the DB), which import_books.py's language resolution now prefers
    over its own title-substring heuristic.

This script only rewrites the JSON files sitting at BOOKS_ROOT -- it does not touch the
database. Run scripts/import/reimport_from_json.py afterward to actually re-import the
updated files: that's what bumps each book's content_version (and therefore its
`contentVersion` in the API), which is how an app that's already cached a book's old JSON
learns there's a new copy to fetch.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402

from app.db import dispose_engine, get_sessionmaker  # noqa: E402


async def _rows(session, ids: list[int] | None):
    sql = """
        SELECT b.id AS book_id, b.content_path, b.language_code,
               COALESCE(
                   (SELECT array_agg(s.title ORDER BY s.sort_order)
                    FROM work_subjects ws JOIN subjects s ON s.id = ws.subject_id
                    WHERE ws.work_id = b.work_id),
                   ARRAY[]::text[]
               ) AS subjects
        FROM books b
        WHERE b.content_path IS NOT NULL
    """
    params = {}
    if ids:
        sql += " AND b.id = ANY(:ids)"
        params["ids"] = ids
    return (await session.execute(text(sql), params)).all()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("books_root", type=Path, help="directory of {id}.json files to rewrite in place")
    parser.add_argument("--ids", help="comma-separated book ids (for a dry run on a few)")
    parser.add_argument("--limit", type=int, help="rewrite at most N files")
    args = parser.parse_args()

    ids = [int(i) for i in args.ids.split(",")] if args.ids else None

    async with get_sessionmaker()() as session:
        rows = await _rows(session, ids)
    if args.limit:
        rows = rows[: args.limit]

    if not rows:
        print("no matching books found", file=sys.stderr)
        return 1

    updated = unchanged = skipped_missing = failed = 0
    for row in rows:
        path = args.books_root / row.content_path
        if not path.is_file():
            skipped_missing += 1
            continue
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            failed += 1
            continue

        subjects = list(row.subjects)
        metadata = content.setdefault("metadata", {})
        if metadata.get("collection") == subjects and metadata.get("language") == row.language_code:
            unchanged += 1
            continue

        metadata["collection"] = subjects
        metadata["language"] = row.language_code
        path.write_text(
            json.dumps(content, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        updated += 1

    print(
        f"updated={updated} unchanged={unchanged} "
        f"skipped_missing_file={skipped_missing} failed={failed} total={len(rows)}"
    )
    await dispose_engine()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
