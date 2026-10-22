"""Request bodies of the account and sync endpoints (docs/accounts_sync_spec.md)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class StartIn(BaseModel):
    email: str = Field(..., max_length=320)
    language: Literal["ar", "fa", "en"] = "ar"


class VerifyIn(BaseModel):
    email: str = Field(..., max_length=320)
    code: str = Field(..., max_length=16)
    deviceId: str = Field(..., min_length=1, max_length=64)
    deviceName: str | None = Field(None, max_length=64)
    platform: Literal["ios", "android", "web"] | None = None


class RefreshIn(BaseModel):
    refreshToken: str = Field(..., max_length=200)
