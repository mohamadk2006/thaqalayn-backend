#!/usr/bin/env python3
"""Load database/seeds/collection_map.json into shamela_collections.

Idempotent: re-running updates the taxonomy assignment for each raw collection string
without touching book rows. That separation is deliberate — the mapping is a judgement
call that will be revised, and revising it must never require re-importing 18,798 books'
worth of content.

Usage:
    python scripts/import/load_collection_map.py [--file database/seeds/collection_map.json]
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

UPSERT = text("""
    INSERT INTO shamela_collections
        (raw, normalized, subject_id, tradition, madhhab, format, language_hint, book_count)
    VALUES
        (:raw, :normalized, :subject, cast(:tradition as tradition), :madhhab,
         cast(:format as book_format), :language_hint, :book_count)
    ON CONFLICT (raw) DO UPDATE SET
        normalized    = EXCLUDED.normalized,
        subject_id    = EXCLUDED.subject_id,
        tradition     = EXCLUDED.tradition,
        madhhab       = EXCLUDED.madhhab,
        format        = EXCLUDED.format,
        language_hint = EXCLUDED.language_hint,
        book_count    = EXCLUDED.book_count
""")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file", type=Path, default=Path("database/seeds/collection_map.json")
    )
    args = parser.parse_args()

    entries = json.loads(args.file.read_text(encoding="utf-8"))
    async with get_sessionmaker()() as session:
        await session.execute(
            UPSERT,
            [
                {
                    "raw": e["raw"],
                    "normalized": e["normalized"],
                    "subject": e["subject"],
                    "tradition": e["tradition"],
                    "madhhab": e["madhhab"],
                    "format": e["format"],
                    "language_hint": e["language_hint"],
                    "book_count": e["book_count"],
                }
                for e in entries
            ],
        )
        await session.commit()

        rows = (await session.execute(text("""
            SELECT coalesce(s.title, '(unmapped)') AS subject,
                   count(*) AS collections,
                   sum(c.book_count) AS books
            FROM shamela_collections c
            LEFT JOIN subjects s ON s.id = c.subject_id
            GROUP BY 1 ORDER BY 3 DESC
        """))).all()

    print(f"loaded {len(entries)} collections\n")
    print(f"{'subject':28} {'collections':>12} {'books':>8}")
    for subject, collections, books in rows:
        print(f"{subject:28} {collections:>12} {books:>8}")

    await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
