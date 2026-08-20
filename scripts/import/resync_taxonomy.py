#!/usr/bin/env python3
"""Re-sync works' subject_id from shamela_collections.

Exists specifically so that revising the collection→subject mapping never requires
re-importing book content. A work's subject_id is set once, at import time, from
whatever the collection mapping said then; if the mapping is corrected afterward, every
work imported *before* the fix is stuck with the old, wrong value until something
re-derives it.

This is that something. It never touches books, pages, or content_path -- only
works.subject_id, re-derived from each work's own books' collection_id. Idempotent and
safe to run any time, including while an import is in progress: it only reads
collection_id off books that already exist, and only writes to works.

Usage:
    python scripts/import/resync_taxonomy.py [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402

from app.db import dispose_engine, get_sessionmaker  # noqa: E402

# One representative book per work (the lowest id, matching _work_collection_raw's own
# convention in catalog_service.py) supplies the subject -- volumes of one work should
# always share a collection, so which one is picked rarely matters in practice.
RESYNC_SQL = text("""
    WITH representative_book AS (
        SELECT DISTINCT ON (work_id) work_id, collection_id
        FROM books
        WHERE collection_id IS NOT NULL
        ORDER BY work_id, id
    )
    UPDATE works w
    SET subject_id = sc.subject_id
    FROM representative_book rb
    JOIN shamela_collections sc ON sc.id = rb.collection_id
    WHERE w.id = rb.work_id
      AND w.subject_id IS DISTINCT FROM sc.subject_id
    RETURNING w.id
""")

COUNT_SQL = text("""
    WITH representative_book AS (
        SELECT DISTINCT ON (work_id) work_id, collection_id
        FROM books
        WHERE collection_id IS NOT NULL
        ORDER BY work_id, id
    )
    SELECT count(*)
    FROM works w
    JOIN representative_book rb ON rb.work_id = w.id
    JOIN shamela_collections sc ON sc.id = rb.collection_id
    WHERE w.subject_id IS DISTINCT FROM sc.subject_id
""")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report the count, change nothing")
    args = parser.parse_args()

    async with get_sessionmaker()() as session:
        if args.dry_run:
            stale = await session.scalar(COUNT_SQL)
            print(f"{stale} work(s) would be updated (dry run -- nothing changed)")
        else:
            updated = (await session.execute(RESYNC_SQL)).rowcount
            await session.commit()
            print(f"resynced {updated} work(s) to the current collection mapping")

    await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
