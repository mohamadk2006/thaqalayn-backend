#!/usr/bin/env python3
"""Populate the database from already-converted, already-validated v2 JSON files --
skipping the .abx -> JSON conversion step import_books.py normally does.

For deploying a corpus that was already converted and validated elsewhere (e.g. on a
dev machine) and rsynced straight to the target's BOOKS_ROOT: the JSON is already
sitting where it needs to be on disk, so there's no reason to burn CPU reconverting
18,798 real source files a second time on the deploy target, and doing so would also
mean re-validating structural correctness that's already been checked.

Critically, this still goes through import_books.py's _import_content() -- the same
idempotency check and ON CONFLICT upsert logic a normal import uses -- rather than a
wholesale table replace (e.g. a raw pg_dump/restore of a freshly-imported database).
That distinction matters on a database with real admin-panel state: is_featured,
is_published, and any hand-edited description on an existing book row are preserved by
an upsert and would be silently wiped out by a blind restore.

Usage:
    python scripts/import/reimport_from_json.py <books-root> [--limit N] [--ids 1,2,3]
                                                 [--force]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

IMPORT_BOOKS = Path(__file__).resolve().parent / "import_books.py"
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("import_books", IMPORT_BOOKS)
import_books = importlib.util.module_from_spec(_spec)
sys.modules["import_books"] = import_books
_spec.loader.exec_module(import_books)

from app.db import dispose_engine, get_sessionmaker  # noqa: E402


async def reimport_one(session, path: Path, run_id: str, books_root: Path, force: bool) -> str:
    """Load one pre-converted JSON file and hand it to the shared upsert logic. Never
    raises -- a malformed file is logged as a failure via _import_content()'s own
    exception handling, same as a normal import."""
    book_id_str = path.stem
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        await session.execute(
            import_books.text("""
                INSERT INTO import_log (run_id, book_id, source_file, status, stage, message)
                VALUES (:run, :bid, :src, 'failed', 'read_json', :msg)
            """),
            {"run": run_id, "bid": int(book_id_str) if book_id_str.isdigit() else None,
             "src": path.name, "msg": str(exc)},
        )
        await session.commit()
        return "failed"

    return await import_books._import_content(
        session, content, book_id_str, path.name, run_id, books_root, force
    )


async def _worker(queue, counts, counts_lock, run_id, books_root, force) -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        while True:
            try:
                path = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                result = await reimport_one(session, path, run_id, books_root, force)
            finally:
                queue.task_done()
            async with counts_lock:
                counts[result] += 1


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("books_root", type=Path, help="directory of already-converted {id}.json files")
    parser.add_argument("--limit", type=int, help="reimport at most N files")
    parser.add_argument("--ids", help="comma-separated list of specific book ids")
    parser.add_argument("--force", action="store_true", help="re-import even if unchanged")
    parser.add_argument("--concurrency", type=int, default=12)
    args = parser.parse_args()

    os.environ["DB_POOL_SIZE"] = str(args.concurrency)
    os.environ["DB_MAX_OVERFLOW"] = "2"

    books_root = args.books_root
    if args.ids:
        wanted = {i.strip() for i in args.ids.split(",")}
        sources = sorted(p for p in books_root.glob("*.json") if p.stem in wanted)
    else:
        sources = sorted(
            books_root.glob("*.json"),
            key=lambda p: int(p.stem) if p.stem.isdigit() else 0,
        )
    if args.limit:
        sources = sources[: args.limit]

    if not sources:
        print(f"no .json files matched in {books_root}", file=sys.stderr)
        return 1

    run_id = f"reimport-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    print(f"run {run_id}: {len(sources)} files from {books_root} (concurrency={args.concurrency})")

    queue: asyncio.Queue[Path] = asyncio.Queue()
    for source in sources:
        queue.put_nowait(source)

    counts = {"ok": 0, "skipped": 0, "failed": 0}
    counts_lock = asyncio.Lock()
    started = datetime.now(UTC)

    async def monitor() -> None:
        while True:
            await asyncio.sleep(15)
            async with counts_lock:
                n = counts["ok"] + counts["skipped"] + counts["failed"]
                snapshot = dict(counts)
            if n == 0:
                continue
            elapsed = (datetime.now(UTC) - started).total_seconds()
            print(f"  {n}/{len(sources)}  ok={snapshot['ok']} "
                  f"skipped={snapshot['skipped']} failed={snapshot['failed']}  "
                  f"({n / elapsed:.1f}/s)")
            if n >= len(sources):
                return

    monitor_task = asyncio.create_task(monitor())
    workers = [
        asyncio.create_task(_worker(queue, counts, counts_lock, run_id, books_root, args.force))
        for _ in range(args.concurrency)
    ]
    await asyncio.gather(*workers)
    monitor_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await monitor_task

    await dispose_engine()
    print(f"\ndone: {counts['ok']} imported, {counts['skipped']} skipped, {counts['failed']} failed")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
