"""Serve single pages and the table of contents of a book straight from its JSON file,
for reading without a full download.

Parsing a book's JSON is the expensive part (tens of MB for large books), and a reader
flipping pages requests the same book repeatedly, so parsed books are kept in a small
LRU keyed by (path, mtime, size) -- a re-imported or edited file naturally misses. Kept
tiny on purpose: the VPS is small and each entry is a whole parsed book.
"""

from __future__ import annotations

import asyncio
import bisect
import json
from collections import OrderedDict
from pathlib import Path

_CACHE_SIZE = 4
_cache: OrderedDict[tuple[str, int, int], "_ParsedBook"] = OrderedDict()


class _ParsedBook:
    def __init__(self, content: dict) -> None:
        pages = sorted(content.get("pages", []), key=lambda p: p["sequence"])
        self.pages = pages
        self.by_sequence = {p["sequence"]: i for i, p in enumerate(pages)}
        page_seq_by_id = {p["id"]: p["sequence"] for p in pages}
        self.toc = sorted(content.get("toc", []), key=lambda e: e.get("order", 0))
        self.toc_sequences = [page_seq_by_id.get(e.get("pageId")) for e in self.toc]
        # Same "which chapter is this page in" rule paging.paginate() applies at import
        # time: the last TOC entry at or before the page.
        resolved = [(s, e) for s, e in zip(self.toc_sequences, self.toc) if s is not None]
        self._resolved = resolved
        self._resolved_seqs = [s for s, _ in resolved]

    def section_title(self, sequence: int) -> str | None:
        i = bisect.bisect_right(self._resolved_seqs, sequence)
        return self._resolved[i - 1][1].get("title") if i else None


def _load_sync(path: Path) -> _ParsedBook | None:
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None
    hit = _cache.get(key)
    if hit is not None:
        _cache.move_to_end(key)
        return hit
    try:
        parsed = _ParsedBook(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, KeyError):
        return None
    _cache[key] = parsed
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)
    return parsed


async def load_book(path: Path) -> _ParsedBook | None:
    # Parsing a large book would otherwise stall the event loop for every other request.
    return await asyncio.to_thread(_load_sync, path)


def get_page(
    book: _ParsedBook, sequence: int
) -> tuple[dict, int | None, int | None, str | None] | None:
    idx = book.by_sequence.get(sequence)
    if idx is None:
        return None
    prev_seq = book.pages[idx - 1]["sequence"] if idx > 0 else None
    next_seq = book.pages[idx + 1]["sequence"] if idx + 1 < len(book.pages) else None
    return book.pages[idx], prev_seq, next_seq, book.section_title(sequence)


def get_toc(book: _ParsedBook) -> list[dict]:
    page_number_by_seq = {p["sequence"]: p.get("pageNumber") for p in book.pages}
    return [
        {
            "id": e.get("id", ""),
            "order": e.get("order", 0),
            "title": e.get("title", ""),
            "pageSequence": seq,
            "pageNumber": page_number_by_seq.get(seq) if seq is not None else e.get("pageNumber"),
        }
        for seq, e in zip(book.toc_sequences, book.toc)
    ]
