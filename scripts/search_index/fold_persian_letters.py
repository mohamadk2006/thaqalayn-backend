"""Fold گ پ چ ژ into ك ب ج ز in every page's stored search index, after migration 5e7a2c9d41b3.

    python scripts/search_index/fold_persian_letters.py run [--batch N] [--from-id ID]
    python scripts/search_index/fold_persian_letters.py status

The index already holds each page's normalized words, so no book file is read: each page
that still has one of the letters gets persian_fold_tsvector() of its own index, which is
what indexing its text afresh with the new arabic_normalize() gives. Runs while the API
keeps serving, one committed batch of page ids at a time; re-running is safe (a folded
page no longer matches) and --from-id resumes. Until a page is folded, a search spelt the
Arabic way does not find it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402

from app.db import get_engine  # noqa: E402

LETTERS = "[گپچژ]"  # گ پ چ ژ

FOLD_BATCH_SQL = f"""
UPDATE pages SET search_tsv = persian_fold_tsvector(search_tsv)
WHERE id >= :lo AND id < :hi AND search_tsv::text ~ '{LETTERS}'
"""


async def run(batch: int, from_id: int | None) -> int:
    engine = get_engine()
    async with engine.connect() as conn:
        lo_id, hi_id = (await conn.execute(text("SELECT min(id), max(id) FROM pages"))).one()
    if lo_id is None:
        print("no pages")
        return 0
    start = time.monotonic()
    folded = 0
    lo = max(lo_id, from_id or lo_id)
    while lo <= hi_id:
        hi = lo + batch
        async with engine.begin() as conn:
            folded += (await conn.execute(text(FOLD_BATCH_SQL), {"lo": lo, "hi": hi})).rowcount
        done = (hi - lo_id) / (hi_id - lo_id + 1)
        elapsed = time.monotonic() - start
        eta = elapsed / done * (1 - done) if done else 0
        print(f"ids < {hi}: {folded} pages folded, {min(done, 1):.1%} "
              f"({elapsed / 60:.0f} min, about {eta / 60:.0f} min left)", flush=True)
        lo = hi
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text("VACUUM ANALYZE pages"))
    print(f"DONE: {folded} pages folded in {(time.monotonic() - start) / 60:.0f} min", flush=True)
    return 0


async def status() -> int:
    async with get_engine().connect() as conn:
        left = await conn.scalar(text(f"SELECT count(*) FROM pages WHERE search_tsv::text ~ '{LETTERS}'"))
    print(f"{left} pages still hold گ پ چ ژ in their index")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument("--batch", type=int, default=20000, help="page ids per committed batch")
    r.add_argument("--from-id", type=int, default=None, help="resume from this page id")
    sub.add_parser("status")
    args = parser.parse_args()
    if args.command == "run":
        return asyncio.run(run(args.batch, args.from_id))
    return asyncio.run(status())


if __name__ == "__main__":
    raise SystemExit(main())
