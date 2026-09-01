"""GET /api/search — full-library Arabic search."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas.catalog import PageEnvelope
from app.schemas.search import SearchHit
from app.services import search_service

router = APIRouter(tags=["search"])


@router.get("/search", response_model=PageEnvelope[SearchHit])
async def search(
    q: str = Query(..., min_length=1, description="Arabic search query"),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    subject: list[str] | None = Query(None, description="One or more subject category IDs"),
    language: list[str] | None = Query(None, description="One or more language codes"),
    author: list[int] | None = Query(None, description="One or more author IDs"),
    authorName: list[str] | None = Query(
        None, description="One or more author names (use when the ID isn't known)"
    ),
    work: int | None = None,
    session: AsyncSession = Depends(get_session),
) -> PageEnvelope[SearchHit]:
    items, total = await search_service.search(
        session, query=q, page=page, limit=limit,
        subject_ids=subject, languages=language,
        author_ids=author, author_names=authorName, work_id=work,
    )
    return PageEnvelope(page=page, limit=limit, total=total, items=items)
