"""GET /api/books, /api/books/{id}, /api/books/{id}/download, /api/books/{id}/cover,
/api/books/{id}/toc, /api/books/{id}/pages/{sequence}."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.models import Book
from app.schemas.catalog import BookOut, PageEnvelope
from app.schemas.reader import PageResponse, TocResponse
from app.services import catalog_service, reader_service

router = APIRouter(tags=["books"])


@router.get("/books", response_model=PageEnvelope[BookOut])
async def list_books(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    subject: str | None = None,
    library: int | None = None,
    language: str | None = None,
    author: int | None = None,
    work: int | None = None,
    session: AsyncSession = Depends(get_session),
) -> PageEnvelope[BookOut]:
    items, total = await catalog_service.list_books(
        session, page=page, limit=limit,
        subject_id=subject, library_id=library, language=language,
        author_id=author, work_id=work,
    )
    return PageEnvelope(page=page, limit=limit, total=total, items=items)


@router.get("/books/{book_id}", response_model=BookOut)
async def get_book(book_id: int, session: AsyncSession = Depends(get_session)) -> BookOut:
    book = await catalog_service.get_book(session, book_id)
    if book is None:
        raise HTTPException(404, detail="book not found")
    return book


def _resolve_under_root(root: Path, relative: str) -> Path:
    """Resolve `relative` against `root` and refuse anything that escapes it.

    `relative` here always comes from our own database, written by our own importer —
    not from the request — so this isn't defending against a live attack payload. It's
    defending against what the request spec explicitly calls for: the endpoint must only
    ever serve registered library files, verified structurally, not just assumed safe
    because the source is trusted today.
    """
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise HTTPException(500, detail="resolved content path escapes the library root")
    return candidate


@router.get("/books/{book_id}/download")
async def download_book(
    book_id: int,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    book = (
        await session.execute(
            select(Book.content_path, Book.title).where(
                Book.id == book_id, Book.is_published.is_(True)
            )
        )
    ).first()
    if book is None or not book.content_path:
        raise HTTPException(404, detail="book not found")

    path = _resolve_under_root(settings.books_root, book.content_path)
    if not path.is_file():
        raise HTTPException(404, detail="content file missing on disk")

    return FileResponse(
        path,
        media_type="application/json",
        filename=f"{book_id}.json",
    )


@router.get("/books/{book_id}/cover")
async def download_cover(
    book_id: int,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    """404s for every book today — no cover images exist in the source material (per the
    project brief: covers are deferred). The endpoint exists now so the client can wire
    up the URL and treat 404 as "show a placeholder" from day one."""
    cover_path = await session.scalar(
        select(Book.cover_path).where(Book.id == book_id, Book.is_published.is_(True))
    )
    if not cover_path:
        raise HTTPException(404, detail="no cover for this book")

    path = _resolve_under_root(settings.covers_root, cover_path)
    if not path.is_file():
        raise HTTPException(404, detail="cover file missing on disk")

    return FileResponse(path)


async def _load_published_book(session: AsyncSession, settings: Settings, book_id: int):
    row = (
        await session.execute(
            select(Book.content_path, Book.content_version, Book.page_count).where(
                Book.id == book_id, Book.is_published.is_(True)
            )
        )
    ).first()
    if row is None or not row.content_path:
        raise HTTPException(404, detail="book not found")
    path = _resolve_under_root(settings.books_root, row.content_path)
    parsed = await reader_service.load_book(path)
    if parsed is None:
        raise HTTPException(404, detail="content file missing or unreadable on disk")
    return row, parsed


@router.get("/books/{book_id}/pages/{sequence}", response_model=PageResponse)
async def get_book_page(
    book_id: int,
    sequence: int,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> PageResponse:
    """One page of a book, for reading without downloading it. `sequence` is the page's
    position in the book (the `sequence` field of a search hit / of the book JSON), not
    its printed label, which is not unique within a book."""
    row, parsed = await _load_published_book(session, settings, book_id)
    found = reader_service.get_page(parsed, sequence)
    if found is None:
        raise HTTPException(404, detail="page not found")
    page, prev_seq, next_seq, section_title = found
    return PageResponse(
        bookId=str(book_id), contentVersion=row.content_version,
        pageCount=len(parsed.pages), page=page, sectionTitle=section_title,
        prevSequence=prev_seq, nextSequence=next_seq,
    )


@router.get("/books/{book_id}/toc", response_model=TocResponse)
async def get_book_toc(
    book_id: int,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> TocResponse:
    row, parsed = await _load_published_book(session, settings, book_id)
    return TocResponse(
        bookId=str(book_id), contentVersion=row.content_version,
        pageCount=len(parsed.pages), entries=reader_service.get_toc(parsed),
    )
