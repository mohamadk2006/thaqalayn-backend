"""Response of GET /api/search/meaning."""

from __future__ import annotations

from pydantic import BaseModel


class MeaningHit(BaseModel):
    bookId: str
    title: str
    author: str | None
    volume: str | None  # as the book's metadata gives it
    page: str | None  # the printed page label
    sequence: int  # the page's position in the book: GET /api/books/{bookId}/pages/{sequence}
    text: str  # the passage itself (a ~220-word chunk of that page)
    score: float  # similarity, 0-1: higher is closer in meaning


class MeaningOut(BaseModel):
    query: str
    items: list[MeaningHit]
