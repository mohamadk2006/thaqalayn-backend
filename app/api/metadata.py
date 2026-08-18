"""GET /api/authors, /api/categories, /api/languages.

'categories' in the URL, matching the project brief's endpoint name, even though the
underlying table is `subjects` — the naming split (subject vs. category) exists because
Amendment 1 introduced subject/tradition/format as separate axes, but the original brief
had already named the endpoint /api/categories and there was no reason to break that."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas.catalog import AuthorOut, LanguageOut, SubjectOut
from app.services import catalog_service

router = APIRouter(tags=["metadata"])


@router.get("/authors", response_model=list[AuthorOut])
async def list_authors(session: AsyncSession = Depends(get_session)) -> list[AuthorOut]:
    return await catalog_service.list_authors(session)


@router.get("/categories", response_model=list[SubjectOut])
async def list_categories(session: AsyncSession = Depends(get_session)) -> list[SubjectOut]:
    return await catalog_service.list_subjects(session)


@router.get("/languages", response_model=list[LanguageOut])
async def list_languages(session: AsyncSession = Depends(get_session)) -> list[LanguageOut]:
    return await catalog_service.list_languages(session)
