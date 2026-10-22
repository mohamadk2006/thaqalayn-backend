"""The signed-in user's synced notes, highlights, marks, bookmarks and reading positions
(docs/accounts_sync_spec.md §4).

    GET  /api/sync/changes?since=<cursor>&limit=500  -> {cursor, hasMore, items}
    POST /api/sync/push  {deviceId, items: [...]}    -> {results: [...]}
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.account import signed_in
from app.api.errors import ApiError
from app.db import get_session
from app.services import auth_service as auth
from app.services import sync_service

router = APIRouter(tags=["sync"])


@router.get("/sync/changes")
async def changes(
    since: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=1000),
    who: auth.Caller = Depends(signed_in),
    session: AsyncSession = Depends(get_session),
):
    return await sync_service.pull(session, who.user.id, since, limit)


@router.post("/sync/push")
async def push(
    request: Request,
    who: auth.Caller = Depends(signed_in),
    session: AsyncSession = Depends(get_session),
):
    raw = await request.body()
    if len(raw) > sync_service.MAX_BODY_BYTES:
        raise ApiError(413, "payload_too_large")
    try:
        body = json.loads(raw)
        items = body["items"]
        device_id = str(body.get("deviceId") or who.login.device_id)[:64]
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ApiError(422, "body_invalid") from None
    if len(items) > sync_service.MAX_ITEMS:
        raise ApiError(413, "payload_too_large")
    return {"results": await sync_service.push(session, who.user.id, device_id, items)}
