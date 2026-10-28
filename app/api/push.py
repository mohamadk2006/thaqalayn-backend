"""Push notifications: the app registers its device token and chooses what it wants
(docs/push_api.md). Signed in only; the token belongs to the login's device."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.account import signed_in
from app.config import Settings, get_settings
from app.db import get_session
from app.services import auth_service as auth
from app.services import push_service

router = APIRouter(prefix="/push", tags=["push"])


class DeviceIn(BaseModel):
    token: str = Field(..., min_length=16, max_length=256, pattern=r"^[0-9a-fA-F]+$")
    environment: Literal["production", "sandbox"] = "production"
    language: Literal["ar", "fa", "en"] = "ar"
    timezone: str = Field("UTC", max_length=64)  # an IANA name, e.g. "Asia/Beirut"
    appVersion: str | None = Field(None, max_length=32)


class PreferencesIn(BaseModel):
    chatAnswer: bool | None = None
    bookUpdates: bool | None = None
    newBooks: bool | None = None


def _out(prefs: dict[str, bool]) -> dict:
    return {
        "chatAnswer": prefs["chat_answer"],
        "bookUpdates": prefs["book_updates"],
        "newBooks": prefs["new_books"],
    }


@router.put("/device")
async def register_device(
    body: DeviceIn,
    who: auth.Caller = Depends(signed_in),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    """This login's device can receive pushes at `token` (call it after sign-in, again when the
    token or the language or time zone changes). The device is the one the login was made on."""
    await push_service.register_device(
        session,
        who.user.id,
        who.login.device_id,
        body.token.lower(),
        environment=body.environment,
        language=body.language,
        timezone=body.timezone,
        app_version=body.appVersion,
    )
    configured = push_service.APNs(settings).configured
    return {"registered": True, "pushEnabled": configured, "preferences": _out(
        await push_service.preferences(session, who.user.id)
    )}  # fmt: skip


@router.delete("/device", status_code=204)
async def unregister_device(
    who: auth.Caller = Depends(signed_in), session: AsyncSession = Depends(get_session)
):
    """Stop pushing to this login's device (signing out does this too)."""
    await push_service.remove_device(session, who.user.id, who.login.device_id)
    return Response(status_code=204)


@router.get("/preferences")
async def get_preferences(
    who: auth.Caller = Depends(signed_in), session: AsyncSession = Depends(get_session)
):
    return _out(await push_service.preferences(session, who.user.id))


@router.put("/preferences")
async def put_preferences(
    body: PreferencesIn,
    who: auth.Caller = Depends(signed_in),
    session: AsyncSession = Depends(get_session),
):
    """Switch kinds on or off; the fields you leave out stay as they are."""
    changes = {
        "chat_answer": body.chatAnswer,
        "book_updates": body.bookUpdates,
        "new_books": body.newBooks,
    }
    return _out(
        await push_service.set_preferences(
            session, who.user.id, {k: v for k, v in changes.items() if v is not None}
        )
    )
