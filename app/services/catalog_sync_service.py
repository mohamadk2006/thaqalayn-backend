"""What changed in the catalog: per-category / per-library versions and the book change feed.

Backs GET /api/catalog/version and GET /api/books/changes. The bookkeeping is done by
database triggers (see the change-tracking migration): every write that could alter what
GET /api/books returns leaves a sequence number behind, whichever code path made it.

A version is derived, not stored, wherever possible: a category's version is the newest of
its own "stamp" (written when a book joins or leaves it) and the change numbers of the
books currently in it. So it moves exactly when its contents move, and a busy importer
never contends on a shared counter row.

Every read here only sees rows written by transactions older than every transaction still
running (`HORIZON`). Sequence numbers are handed out before commit, so a slow transaction
can commit after a faster, later one; without this a client could advance its cursor past
a change that had not committed yet and never receive it. The cost is only a short delay
before a change becomes visible.
"""

from __future__ import annotations

import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import catalog_service

HORIZON = "pg_snapshot_xmin(pg_current_snapshot())"
TOMBSTONE_DAYS = 180
_PURGE_EVERY_SECONDS = 3600
_last_purge = 0.0


def _v(prefix: str, seq: int) -> str:
    return f"{prefix}-{seq}"


async def latest_seq(session: AsyncSession) -> int:
    return await session.scalar(
        text(f"SELECT COALESCE(max(seq), 0) FROM book_changes WHERE xid < {HORIZON}")
    )


async def get_versions(session: AsyncSession) -> dict:
    stamps = {
        (r.scope, r.key): r.seq
        for r in (await session.execute(
            text(f"SELECT scope, key, seq FROM catalog_stamps WHERE xid < {HORIZON}")
        )).all()
    }

    async def newest_book_change(join_table: str, key_col: str) -> dict[str, int]:
        rows = (await session.execute(text(f"""
            SELECT CAST(g.{key_col} AS text) AS k, max(bc.seq) AS seq
            FROM {join_table} g
            JOIN books b ON b.work_id = g.work_id
            JOIN book_changes bc ON bc.book_id = b.id AND bc.xid < {HORIZON}
            GROUP BY g.{key_col}
        """))).all()
        return {r.k: r.seq for r in rows}

    async def counts(join_table: str, key_col: str) -> dict[str, int]:
        rows = (await session.execute(text(f"""
            SELECT CAST(g.{key_col} AS text) AS k, count(b.id) AS n
            FROM {join_table} g
            JOIN books b ON b.work_id = g.work_id AND b.is_published
            GROUP BY g.{key_col}
        """))).all()
        return {r.k: r.n for r in rows}

    subject_books = await newest_book_change("work_subjects", "subject_id")
    subject_counts = await counts("work_subjects", "subject_id")
    library_books = await newest_book_change("library_works", "library_id")
    library_counts = await counts("library_works", "library_id")

    featured_books = await session.scalar(text(f"""
        SELECT COALESCE(max(bc.seq), 0)
        FROM book_changes bc
        JOIN books b ON b.id = bc.book_id
        JOIN works w ON w.id = b.work_id AND w.is_featured
        WHERE bc.xid < {HORIZON}
    """))

    subject_ids = [r[0] for r in (await session.execute(text("SELECT id FROM subjects ORDER BY sort_order"))).all()]
    library_ids = [str(r[0]) for r in (await session.execute(text("SELECT id FROM libraries ORDER BY id"))).all()]
    seq = await latest_seq(session)

    def group(prefix: str, scope: str, ids: list[str], books: dict, cnts: dict) -> dict:
        return {
            i: {
                "version": _v(prefix, max(stamps.get((scope, i), 0), books.get(i, 0))),
                "count": cnts.get(i, 0),
            }
            for i in ids
        }

    return {
        "categories": {
            "version": _v("c", stamps.get(("categories", ""), 0)),
            "items": group("s", "subject", subject_ids, subject_books, subject_counts),
        },
        "libraries": {
            "version": _v("l", stamps.get(("libraries", ""), 0)),
            "items": group("l", "library", library_ids, library_books, library_counts),
        },
        "books": {"version": _v("b", seq), "cursor": str(seq)},
        "featuredWorks": {"version": _v("f", max(stamps.get(("featured", ""), 0), featured_books))},
    }


async def _purge_old_deletions(session: AsyncSession) -> None:
    """Forget deletion records older than TOMBSTONE_DAYS, remembering the highest sequence
    number dropped so a client whose cursor is older than that is told to reload."""
    global _last_purge
    if time.monotonic() - _last_purge < _PURGE_EVERY_SECONDS:
        return
    _last_purge = time.monotonic()
    await session.execute(text(f"""
        WITH gone AS (
            DELETE FROM book_changes bc
            WHERE bc.changed_at < now() - interval '{TOMBSTONE_DAYS} days'
              AND NOT EXISTS (SELECT 1 FROM books b WHERE b.id = bc.book_id AND b.is_published)
            RETURNING bc.seq
        )
        UPDATE catalog_meta SET value = GREATEST(value, COALESCE((SELECT max(seq) FROM gone), 0))
        WHERE key = 'tombstone_floor'
    """))
    await session.commit()


async def get_changes(session: AsyncSession, since: str | None, limit: int) -> dict:
    await _purge_old_deletions(session)
    current = await latest_seq(session)
    floor = await session.scalar(text("SELECT value FROM catalog_meta WHERE key = 'tombstone_floor'"))

    try:
        since_n = int(since) if since is not None else None
    except ValueError:
        since_n = None
    # Missing, unparseable, "never synced" (< 1), older than what is still remembered, or
    # ahead of the server (a cursor from a different database): the client cannot trust
    # a diff and must reload the whole catalog.
    if since_n is None or since_n < 1 or since_n < floor or since_n > current:
        return {"cursor": str(current), "hasMore": False, "reset": True, "items": []}

    rows = (await session.execute(
        text(f"""
            SELECT book_id, seq FROM book_changes
            WHERE seq > :since AND xid < {HORIZON}
            ORDER BY seq LIMIT :n
        """),
        {"since": since_n, "n": limit + 1},
    )).all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    books = await catalog_service.get_books_by_ids(session, [r.book_id for r in rows])
    items = []
    for r in rows:
        book = books.get(r.book_id)
        if book is not None:
            items.append({"seq": r.seq, "op": "upsert", "book": book.model_dump(mode="json")})
        else:  # deleted, or no longer published: to the catalog it is gone either way
            items.append({"seq": r.seq, "op": "delete", "bookId": str(r.book_id)})

    return {
        "cursor": str(rows[-1].seq) if rows else str(since_n),
        "hasMore": has_more,
        "reset": False,
        "items": items,
    }
