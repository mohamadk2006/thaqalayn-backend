#!/usr/bin/env python3
"""Backfill each book's content JSON with its library membership, for offline display.

Adds `metadata.libraries`: an array of library title strings (the same shape as
`metadata.collection`, which already carries the corrected subject classification --
see backfill_subjects_language.py) -- a work's independent, open-ended library
membership (see app.models.library.Library), separate from its subjects.

Unlike subjects/language, most books will get an empty list here: libraries are grown
over time via the admin panel, not assigned in one pass, so this script is meant to be
re-run whenever the admin wants the downloadable JSON to catch up with the latest
library assignments -- it does not run automatically on every admin edit, the same
tradeoff already made for subjects/language (see that script's own docstring).

This script only rewrites the JSON files sitting at BOOKS_ROOT -- it does not touch the
database. Run scripts/import/reimport_from_json.py afterward to actually re-import the
updated files: that's what bumps each book's content_version (and therefore its
`contentVersion` in the API), which is how an app that's already cached a book's old
JSON learns there's a new copy to fetch.
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
        SELECT b.id AS book_id, b.content_path,
               COALESCE(
                   (SELECT array_agg(l.title ORDER BY l.sort_order)
                    FROM library_works lw JOIN libraries l ON l.id = lw.library_id
                    WHERE lw.work_id = b.work_id),
                   ARRAY[]::text[]
               ) AS libraries
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

    print(f"{len(rows)} books to process", flush=True)
    updated = unchanged = skipped_missing = failed = 0
    for idx, row in enumerate(rows):
        if idx % 1000 == 0:
            print(f"  {idx}/{len(rows)}", flush=True)
        path = args.books_root / row.content_path
        if not path.is_file():
            skipped_missing += 1
            continue
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            failed += 1
            continue

        libraries = list(row.libraries)
        metadata = content.setdefault("metadata", {})
        if metadata.get("libraries") == libraries:
            unchanged += 1
            continue

        metadata["libraries"] = libraries
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
