"""Response schemas for reading a book page-by-page without downloading it.

`page` is the exact same object the downloadable book JSON carries for that page
(id, sequence, pageNumber, pageType, isBlank, printedPage, sourcePageLabel, blocks), so a
client's existing reader/page decoding works unchanged whether a page came from a local
file or from this endpoint.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class PageResponse(BaseModel):
    bookId: str
    contentVersion: int
    pageCount: int
    page: dict[str, Any]
    sectionTitle: str | None
    prevSequence: int | None
    nextSequence: int | None


class TocEntryOut(BaseModel):
    id: str
    order: int
    title: str
    pageSequence: int | None
    pageNumber: str | None


class TocResponse(BaseModel):
    bookId: str
    contentVersion: int
    pageCount: int
    entries: list[TocEntryOut]
