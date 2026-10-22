"""User accounts (sign-in by an emailed one-time code) and the sync store.

See docs/accounts_sync_spec.md. Codes and tokens are stored only as hashes; emails only
in `users` and `email_codes` (the send log keeps a hash). Sync records are opaque JSON
blobs under (user, kind, key): the server reads only the few fields its merge rules need.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

_now = func.now()


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)  # normalized
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class EmailCode(Base):
    """One sign-in code sent to an email. Only the latest one for an email can work; the
    rows also serve as the rate-limit counters (per email and per IP over the last hour)."""

    __tablename__ = "email_codes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    code_hash: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # HMAC-SHA256 with AUTH_CODE_PEPPER
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )
    ip: Mapped[str | None] = mapped_column(INET)

    __table_args__ = (
        Index("ix_email_codes_email_created", "email", text("created_at DESC")),
        Index("ix_email_codes_ip_created", "ip", "created_at"),
    )


class VerifyCall(Base):
    """One call to /api/auth/email/verify, for its per-IP rate limit."""

    __tablename__ = "verify_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ip: Mapped[str | None] = mapped_column(INET)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (Index("ix_verify_calls_ip_created", "ip", "created_at"),)


class AuthSession(Base):
    """A signed-in device: its current access and refresh tokens (hashes only)."""

    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[str] = mapped_column(Text, nullable=False)
    device_name: Mapped[str | None] = mapped_column(Text)
    platform: Mapped[str | None] = mapped_column(Text)
    access_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    refresh_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    access_expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    refresh_expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )
    last_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_auth_sessions_user", "user_id"),)


class UsedRefreshToken(Base):
    """A refresh token that was already exchanged. Seeing it again means it was copied:
    the whole login session is revoked."""

    __tablename__ = "used_refresh_tokens"

    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth_sessions.id", ondelete="CASCADE"), nullable=False
    )
    used_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )


class EmailSend(Base):
    """One attempt to send a code through a provider. Never the code; the email as a hash."""

    __tablename__ = "email_sends"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    email_hash: Mapped[str] = mapped_column(Text, nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )


class SyncRecord(Base):
    """One synced item of a user. `key_lower` is the identity (ids compare case-insensitively);
    `key` is kept as the app first sent it. Every write takes a new `seq` from sync_seq."""

    __tablename__ = "sync_records"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    key_lower: Mapped[str] = mapped_column(Text, primary_key=True)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict | None] = mapped_column(JSONB)
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    order_changed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    device_id: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_sync_records_user_seq", "user_id", "seq"),)
