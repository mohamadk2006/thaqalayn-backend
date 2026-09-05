"""GET /api/works, /api/works/{id}."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas.catalog import PageEnvelope, WorkDetailOut, WorkOut
from app.services import catalog_service

router = APIRouter(tags=["works"])


@router.get("/works", response_model=PageEnvelope[WorkOut])
async def list_works(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    subject: str | None = None,
    language: str | None = None,
    author: int | None = None,
    featured: bool | None = None,
    session: AsyncSession = Depends(get_session),
) -> PageEnvelope[WorkOut]:
    items, total = await catalog_service.list_works(
        session, page=page, limit=limit,
        subject_id=subject, language=language, author_id=author, featured=featured,
    )
    return PageEnvelope(page=page, limit=limit, total=total, items=items)


@router.get("/works/{work_id}", response_model=WorkDetailOut)
async def get_work(work_id: int, session: AsyncSession = Depends(get_session)) -> WorkDetailOut:
    work = await catalog_service.get_work(session, work_id)
    if work is None:
        raise HTTPException(404, detail="work not found")
    return work
