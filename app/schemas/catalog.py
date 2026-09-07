"""Response schemas for the catalog, work, and book endpoints.

Field names and shapes here are a contract already communicated to the iOS session
across three documents (the initial backend brief, and Amendments 1–2 on categories and
works/volumes). Renaming a field here is a breaking change on that side — check the
brief before changing anything under BookOut or WorkOut.

`subjects` (plural) carries the project owner's 39-category classification (see
app.models.library.Subject/WorkSubject) — a work can genuinely belong to more than one,
which replaced the old single subjectId/subjectTitle pair. tradition/madhhab/format were
already removed as separate fields before that: most of that distinction is already
encoded directly in which of the 39 a work falls under.

bookId/workId are emitted as strings, matching the `let bookId: String` already in the
iOS CatalogBook type, even though both are integers internally (the Shamela filename ID).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PageEnvelope[T](BaseModel):
    """Shared pagination shape for every list endpoint, per the API contract:
    { page, limit, total, items }. Never return an unpaginated array — with ~18,800
    books, an unbounded /api/books would be a many-megabyte response by design."""

    page: int
    limit: int
    total: int
    items: list[T]


class AuthorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    deathLabel: str | None = None


class SubjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str


class LanguageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    name: str


class BookOut(BaseModel):
    """One volume — the downloadable unit. Matches CatalogBook in the iOS brief."""

    bookId: str
    workId: str
    workTitle: str
    volume: int | None
    title: str
    author: str
    authorDeath: str | None
    description: str | None
    subjects: list[SubjectOut]
    language: str | None
    publisher: str | None
    shamelaCollection: str | None
    pageFirst: int | None
    pageLast: int | None
    paragraphCount: int | None
    sizeBytes: int
    # No gzip transport yet (Milestone 5 scope): this is a placeholder equal to
    # sizeBytes, not a real compressed size. Real compression is a Milestone 7/VPS
    # concern (GZipMiddleware or Nginx) — flagged here rather than silently guessed.
    downloadBytes: int
    contentVersion: int


class WorkOut(BaseModel):
    """One title, independent of volume count. Matches CatalogWork in Amendment 2."""

    workId: str
    title: str
    author: str
    authorDeath: str | None
    subjects: list[SubjectOut]
    language: str | None
    volumeCount: int
    totalSizeBytes: int
    shamelaCollection: str | None
    isFeatured: bool


class WorkDetailOut(WorkOut):
    """GET /api/works/{workId}: the summary plus every volume, per Amendment 2 —
    volumes are deliberately NOT embedded in the list endpoint's WorkOut."""

    volumes: list[BookOut]
