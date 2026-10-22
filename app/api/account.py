"""Sign-in by email code, tokens, and the signed-in user's account
(docs/accounts_sync_spec.md §1). Errors use the {"code", "detail"} format of app.api.errors.

    POST   /api/auth/email/start      {email, language}            -> 202 {resendAfter, expiresIn}
    POST   /api/auth/email/verify     {email, code, deviceId, ...} -> 200 tokens + user
    POST   /api/auth/refresh          {refreshToken}               -> 200 new tokens
    POST   /api/auth/logout           (bearer)                     -> 204
    GET    /api/account               (bearer)                     -> the user and their devices
    DELETE /api/account/devices/{id}  (bearer)                     -> 204, that device signed out
    DELETE /api/account               (bearer)                     -> 204, account and data deleted
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.schemas.account import RefreshIn, StartIn, VerifyIn
from app.services import auth_service as auth

router = APIRouter(tags=["account"])


async def signed_in(
    authorization: str | None = Header(None),
    session: AsyncSession = Depends(get_session),
) -> auth.Caller:
    return await auth.caller(session, authorization)


@router.post("/auth/email/start", status_code=202)
async def start(
    body: StartIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    answer = await auth.start(session, settings, body.email, body.language, auth.client_ip(request))
    return JSONResponse(answer, status_code=202)


@router.post("/auth/email/verify")
async def verify(
    body: VerifyIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    device = auth.Device(body.deviceId, body.deviceName, body.platform)
    return await auth.verify(
        session, settings, body.email, body.code, device, auth.client_ip(request)
    )


@router.post("/auth/refresh")
async def refresh(body: RefreshIn, session: AsyncSession = Depends(get_session)):
    return await auth.refresh(session, body.refreshToken)


@router.post("/auth/logout", status_code=204)
async def logout(
    who: auth.Caller = Depends(signed_in), session: AsyncSession = Depends(get_session)
):
    await auth.logout(session, who)
    return Response(status_code=204)


@router.get("/account")
async def account(
    who: auth.Caller = Depends(signed_in), session: AsyncSession = Depends(get_session)
):
    return await auth.account(session, who)


@router.delete("/account/devices/{device_id}", status_code=204)
async def sign_out_device(
    device_id: str,
    who: auth.Caller = Depends(signed_in),
    session: AsyncSession = Depends(get_session),
):
    await auth.sign_out_device(session, who, device_id)
    return Response(status_code=204)


@router.delete("/account", status_code=204)
async def delete_account(
    who: auth.Caller = Depends(signed_in), session: AsyncSession = Depends(get_session)
):
    await auth.delete_account(session, who)
    return Response(status_code=204)
