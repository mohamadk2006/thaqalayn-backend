"""GET /api/search — full-library Arabic search."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.schemas.catalog import PageEnvelope
from app.schemas.meaning import MeaningOut
from app.schemas.search import SearchHit
from app.services import auth_service as auth
from app.services import meaning_search, search_service

router = APIRouter(tags=["search"])


@router.get("/search", response_model=PageEnvelope[SearchHit])
async def search(
    q: str = Query(..., min_length=1, description="Arabic search query"),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    subject: list[str] | None = Query(None, description="One or more subject category IDs"),
    library: list[int] | None = Query(None, description="One or more library IDs"),
    language: list[str] | None = Query(None, description="One or more language codes"),
    author: list[int] | None = Query(None, description="One or more author IDs"),
    authorName: list[str] | None = Query(
        None, description="One or more author names (use when the ID isn't known)"
    ),
    work: list[int] | None = Query(
        None,
        description="Work IDs searched in addition to what the other filters select: "
                    "(all the other filters together) OR work. With no other filter, only "
                    "these works."),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> PageEnvelope[SearchHit]:
    items, total = await search_service.search(
        session, query=q, page=page, limit=limit, books_root=settings.books_root,
        subject_ids=subject, library_ids=library, languages=language,
        author_ids=author, author_names=authorName, work_ids=work,
    )
    return PageEnvelope(page=page, limit=limit, total=total, items=items)


@router.get("/search/meaning", response_model=MeaningOut)
async def search_by_meaning(
    request: Request,
    q: str = Query(
        ..., min_length=2, max_length=500, description="A question or a topic, in words"
    ),
    limit: int = Query(20, ge=1, le=50),
    bookId: list[int] | None = Query(None, max_length=50, description="Only these books"),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    """Passages of the library closest in meaning to `q` (not the same words): each with its
    book, author, page and text. No AI-written answer; for that see POST /api/chat."""
    try:
        meaning_search.allow(auth.client_ip(request) or "unknown")
    except meaning_search.TooManySearches:
        return JSONResponse(
            {"code": "too_fast", "detail": "عمليات بحث كثيرة، انتظر دقيقة."},
            status_code=429,
            headers={"Retry-After": "60"},
        )
    if not (settings.qdrant_api_key and settings.embedder_url):
        return JSONResponse({"detail": "البحث بالمعنى غير مفعّل على هذا الخادم."}, status_code=503)
    try:
        items = await meaning_search.search(
            session, settings, q.strip(), limit, [str(b) for b in bookId] if bookId else None
        )
    except RuntimeError:
        return JSONResponse({"detail": "تعذّر البحث الآن، حاول مرة أخرى."}, status_code=502)
    return MeaningOut(query=q.strip(), items=items)
