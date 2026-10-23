"""Search by meaning: the question is embedded (BGE-M3) and the closest passages of the
library come back, with no model writing text. The same vector search the chatbot uses.

The embedder has two CPUs, so at most EMBED_AT_ONCE questions are embedded together, each
address gets RATE_PER_MINUTE searches a minute, and an answer is kept for CACHE_SECONDS.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, defaultdict, deque

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.library import Author, Book
from app.schemas.meaning import MeaningHit
from app.services import chat_service

EMBED_AT_ONCE = 4
RATE_PER_MINUTE = 30
CACHE_SECONDS = 300
CACHE_ENTRIES = 2000

_slots = asyncio.Semaphore(EMBED_AT_ONCE)
_cache: OrderedDict[tuple, tuple[float, list[MeaningHit]]] = OrderedDict()
_recent: dict[str, deque] = defaultdict(deque)


class TooManySearches(Exception):
    pass


def allow(who: str, now: float | None = None) -> None:
    """Counts a search by `who`; raises TooManySearches past RATE_PER_MINUTE a minute."""
    now = time.monotonic() if now is None else now
    times = _recent[who]
    while times and now - times[0] > 60:
        times.popleft()
    if len(times) >= RATE_PER_MINUTE:
        raise TooManySearches
    times.append(now)
    if len(_recent) > 5000:  # forget addresses idle for a minute
        for k in [k for k, v in _recent.items() if not v or now - v[-1] > 60]:
            del _recent[k]


async def search(
    session: AsyncSession,
    settings: Settings,
    query: str,
    limit: int,
    book_ids: list[str] | None = None,
) -> list[MeaningHit]:
    key = (query, limit, tuple(sorted(book_ids or [])))
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        _cache.move_to_end(key)
        return hit[1]

    async with _slots:
        vectors = await asyncio.to_thread(chat_service._embed, settings, [query])
    # one result per page (a page can hold several chunks, even identical ones): ask for more,
    # keep the best chunk of each page
    found = await asyncio.to_thread(
        chat_service._search, settings, vectors, min(limit * 2, 200), book_ids
    )
    passages, pages = [], set()
    for p in found:
        if (p["bookId"], p["sequence"]) not in pages:
            pages.add((p["bookId"], p["sequence"]))
            passages.append(p)
    passages = passages[:limit]

    ids = {int(p["bookId"]) for p in passages if str(p["bookId"]).isdigit()}
    authors = {}
    if ids:
        rows = await session.execute(
            select(Book.id, Author.name)
            .join(Author, Author.id == Book.author_id)
            .where(Book.id.in_(ids))
        )
        authors = dict(rows.all())
    items = [
        MeaningHit(
            bookId=str(p["bookId"]),
            title=p.get("title") or "",
            author=authors.get(int(p["bookId"])) if str(p["bookId"]).isdigit() else None,
            volume=str(p["volume"]) if p.get("volume") not in (None, "") else None,
            page=str(p["page"]) if p.get("page") is not None else None,
            sequence=p["sequence"],
            text=p.get("text") or "",
            score=p.get("score") or 0.0,
        )
        for p in passages
    ]
    _cache[key] = (time.monotonic(), items)
    while len(_cache) > CACHE_ENTRIES:
        _cache.popitem(last=False)
    return items
