#!/usr/bin/env python3
"""Re-derive authors.death_year_hijri for every row from its own death_label.

The original _hijri_year() concatenated every digit in a سنة الوفاة label into one
number, which silently corrupted every author whose label held more than one number --
a range ("142 - 276" -> 142276) or several scholars combined into one compound entry
("1281 - 1312 - 1327 - 1292" -> a 16-digit number that overflows int32 and crashes the
import outright, which is how this was actually found). Fixed in import_books.py to
take the first digit run instead; this backfills every row already written under the
old, wrong logic.

death_label itself is never touched -- it always held the correct original text. Only
the derived integer column is re-computed.

Usage:
    python scripts/import/resync_death_years.py [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402

from app.db import dispose_engine, get_sessionmaker  # noqa: E402


def hijri_year(death_label: str) -> int | None:
    match = re.search(r"\d+", death_label)
    return int(match.group()) if match else None


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report changes, write nothing")
    args = parser.parse_args()

    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(text("SELECT id, death_label, death_year_hijri FROM authors"))
        ).all()

        updates = []
        for author_id, label, current in rows:
            if not label:
                continue
            correct = hijri_year(label)
            if correct != current:
                updates.append((author_id, label, current, correct))

        print(f"{len(rows)} authors checked, {len(updates)} need correction")
        for _id, label, current, correct in updates[:10]:
            print(f"  {label!r:40} {current} -> {correct}")
        if len(updates) > 10:
            print(f"  ... and {len(updates) - 10} more")

        if not args.dry_run and updates:
            await session.execute(
                text("UPDATE authors SET death_year_hijri = :year WHERE id = :id"),
                [{"id": author_id, "year": correct} for author_id, _, _, correct in updates],
            )
            await session.commit()
            print(f"\nupdated {len(updates)} authors")
        elif args.dry_run:
            print("\n(dry run -- nothing written)")

    await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
