"""Build the exact-word search index ahead of the migration that switches to it.

    python scripts/search_index/rebuild_exact.py build [--limit N]   # fill pages_exact
    python scripts/search_index/rebuild_exact.py index               # its indexes/constraints
    python scripts/search_index/rebuild_exact.py status

Runs while the API keeps serving from `pages`; both phases are resumable and re-runnable.
The migration (f41d0c7a9b22) then catches up any book re-imported in between and swaps
pages_exact in -- see app/services/search_reindex.py for the whole design.

Books are written one at a time, in book order, so the finished table keeps each book's
pages physically together; JSON files are read and paginated ahead on a thread pool,
which is what keeps that single writer busy.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import get_engine  # noqa: E402
from app.services import search_reindex as R  # noqa: E402


async def build(limit: int | None, workers: int) -> int:
    books_root = Path(get_settings().books_root)
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text(R.CREATE_TARGET_SQL))
        await conn.execute(text(R.STALE_IN_TARGET_SQL))
        book_ids = [r[0] for r in (await conn.execute(text(R.BOOKS_TO_BUILD_SQL))).all()]
    if limit:
        book_ids = book_ids[:limit]
    print(f"{len(book_ids)} books to build", flush=True)

    problems: list[str] = []
    started = time.monotonic()
    pages_done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # map() yields in submission order, so the writer still goes book by book.
        texts_iter = pool.map(lambda b: (b, R.book_page_texts(books_root, b)), book_ids)
        async with engine.connect() as conn:
            for n, (book_id, texts) in enumerate(texts_iter, 1):
                if texts is None:
                    problems.append(f"{book_id}: JSON file missing or unreadable")
                    continue
                db_seqs = [r[0] for r in (await conn.execute(
                    text(R.PAGE_SEQUENCES_SQL), {"bid": book_id})).all()]
                missing = R.missing_sequences(db_seqs, texts)
                if missing:
                    problems.append(f"{book_id}: {len(missing)} page(s) not in its JSON, e.g. {missing[:5]}")
                    continue
                await conn.execute(text(R.DELETE_BOOK_SQL), {"bid": book_id})
                await conn.execute(text(R.INSERT_BOOK_SQL), R.book_params(book_id, texts))
                await conn.commit()
                pages_done += len(db_seqs)
                if n % 200 == 0 or n == len(book_ids):
                    rate = pages_done / max(time.monotonic() - started, 1e-6)
                    eta = (len(book_ids) - n) * (time.monotonic() - started) / n
                    print(f"{n}/{len(book_ids)} books, {pages_done} pages, "
                          f"{rate:.0f} pages/s, ~{eta / 60:.0f} min left", flush=True)

    for p in problems:
        print("PROBLEM", p, flush=True)
    print(f"done: {len(book_ids) - len(problems)} built, {len(problems)} problems, "
          f"{(time.monotonic() - started) / 60:.1f} min", flush=True)
    return 1 if problems else 0


async def index() -> int:
    engine = get_engine()
    async with engine.connect() as conn:
        indexes = (await conn.execute(text(R.PAGES_INDEXES_SQL))).all()
        constraints = (await conn.execute(text(R.PAGES_CONSTRAINTS_SQL))).all()
        target_has = {r[0] for r in (await conn.execute(text(f"""
            SELECT relname FROM pg_class WHERE oid IN (
                SELECT indexrelid FROM pg_index WHERE indrelid = '{R.TARGET}'::regclass)
            UNION SELECT conname FROM pg_constraint WHERE conrelid = '{R.TARGET}'::regclass
        """))).all()}
        statements = R.index_statements(indexes, constraints, target_has)
        await conn.execute(text("SET maintenance_work_mem = '2GB'"))
        for ddl in statements:
            started = time.monotonic()
            print(ddl, flush=True)
            await conn.execute(text(ddl))
            await conn.commit()
            print(f"  {time.monotonic() - started:.0f}s", flush=True)
        await conn.execute(text(f"ANALYZE {R.TARGET}"))
        await conn.commit()
    print(f"done: {len(statements)} created", flush=True)
    return 0


async def status() -> int:
    async with get_engine().connect() as conn:
        exists = await conn.scalar(text(f"SELECT to_regclass('{R.TARGET}') IS NOT NULL"))
        if not exists:
            print(f"{R.TARGET} does not exist")
            return 0
        row = (await conn.execute(text(f"""
            SELECT (SELECT count(*) FROM pages), (SELECT count(*) FROM {R.TARGET}),
                   pg_size_pretty(pg_total_relation_size('pages')),
                   pg_size_pretty(pg_total_relation_size('{R.TARGET}'))
        """))).one()
        pending = len((await conn.execute(text(R.BOOKS_TO_BUILD_SQL))).all())
    print(f"pages: {row[0]} rows, {row[2]}   {R.TARGET}: {row[1]} rows, {row[3]}   "
          f"books still to build: {pending}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["build", "index", "status"])
    parser.add_argument("--limit", type=int, help="build: only the first N books (for a trial)")
    parser.add_argument("--workers", type=int, default=4, help="build: JSON reader threads")
    args = parser.parse_args()
    if args.command == "build":
        code = asyncio.run(build(args.limit, args.workers))
    elif args.command == "index":
        code = asyncio.run(index())
    else:
        code = asyncio.run(status())
    sys.exit(code)


if __name__ == "__main__":
    main()
