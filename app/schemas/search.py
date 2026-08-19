"""Response schema for GET /api/search.

Per the project brief: book, chapter/section, page, snippet, and relevance -- not just
"book X contains this phrase." Field names match the conventions already established in
catalog.py (bookId/workId as strings, workTitle alongside title) so the client's
existing decoding patterns extend to search results without a separate contract.
"""

from __future__ import annotations

from pydantic import BaseModel


class SearchHit(BaseModel):
    bookId: str
    workId: str
    workTitle: str
    title: str
    author: str
    volume: int | None
    subjectId: str | None
    subjectTitle: str | None
    sectionTitle: str | None  # None when the source has no headings at all
    page: int
    snippet: str
    # Character offsets into `snippet` (not into the full page), matching the
    # ReaderHighlight/matchRange convention the client's own local search already uses --
    # so the same highlighting code path can render either result type. Both null when
    # the regex-based original-text locator couldn't confirm a position (rare
    # tokenization divergence from the SQL side); the snippet itself is still shown,
    # just unhighlighted.
    matchStart: int | None
    matchEnd: int | None
    score: float
