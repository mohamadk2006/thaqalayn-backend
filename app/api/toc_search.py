"""GET /api/toc/search — search chapter headings across every published book."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas.search import TocSearchResponse
from app.services import toc_search_service

router = APIRouter(tags=["search"])


@router.get("/toc/search", response_model=TocSearchResponse)
async def search_toc(
    q: str = Query(..., min_length=1, description="Heading text; sent as typed, folded server-side"),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    subject: list[str] | None = Query(None, description="One or more subject category IDs"),
    library: list[int] | None = Query(None, description="One or more library IDs"),
    language: list[str] | None = Query(None, description="One or more language codes"),
    author: list[int] | None = Query(None, description="One or more author IDs"),
    authorName: list[str] | None = Query(None, description="One or more author names"),
    work: list[int] | None = Query(None, description="One or more work IDs"),
    session: AsyncSession = Depends(get_session),
) -> TocSearchResponse:
    items, total, capped = await toc_search_service.search_toc(
        session, query=q, page=page, limit=limit,
        subject_ids=subject, library_ids=library, languages=language,
        author_ids=author, author_names=authorName, work_ids=work,
    )
    return TocSearchResponse(page=page, limit=limit, total=total, items=items, capped=capped)
