"""GET /api/catalog/version and GET /api/books/changes — let an app cheaply learn what
changed in the catalog and fetch only that.

Registered before the books router: `/books/changes` would otherwise be captured by
`/books/{book_id}`.
"""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.services import catalog_sync_service

router = APIRouter(tags=["catalog-sync"])


def _etag(body: dict) -> str:
    digest = hashlib.sha1(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return f'W/"{digest[:20]}"'


@router.get("/catalog/version")
async def catalog_version(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    """Called once per app launch, so it stays small. The body's ETag lets an unchanged
    catalog cost the app a 304 and no body at all."""
    body = await catalog_sync_service.get_versions(session)
    etag = _etag(body)
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    sent = [t.strip() for t in request.headers.get("if-none-match", "").split(",")]
    if etag in sent or etag.removeprefix("W/") in [s.removeprefix("W/") for s in sent]:
        return Response(status_code=304, headers=headers)
    return JSONResponse(body, headers=headers)


@router.get("/books/changes")
async def books_changes(
    since: str | None = Query(None, description="The cursor from the previous response"),
    limit: int = Query(500, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    body = await catalog_sync_service.get_changes(session, since, limit)
    return JSONResponse(body, headers={"Cache-Control": "no-store"})
