"""Push notifications: the devices that can receive them, what each user wants, the queue of
notifications waiting to be sent, and the book versions already announced.

A device token belongs to one login's device of one user and goes with the account (ON DELETE
CASCADE). Tokens are never logged.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

KINDS = ("chat_answer", "book_updates", "new_books")
DEFAULTS = {"chat_answer": True, "book_updates": True, "new_books": False}


class PushDevice(Base):
    __tablename__ = "push_devices"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[str] = mapped_column(Text, nullable=False)  # the login's device id
    platform: Mapped[str] = mapped_column(Text, nullable=False, server_default="ios")
    token: Mapped[str] = mapped_column(Text, nullable=False)
    environment: Mapped[str] = mapped_column(Text, nullable=False, server_default="production")
    language: Mapped[str] = mapped_column(Text, nullable=False, server_default="ar")
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="UTC")
    app_version: Mapped[str | None] = mapped_column(Text)
    # What iOS says about notifications for the app: notDetermined, denied, authorized, provisional
    permission: Mapped[str] = mapped_column(Text, nullable=False, server_default="authorized")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_ok_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("platform", "token", name="uq_push_devices_token"),
        UniqueConstraint("user_id", "device_id", "platform", name="uq_push_devices_login"),
        Index("ix_push_devices_user", "user_id"),
    )


class PushPreference(Base):
    __tablename__ = "push_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))  # digests

    __table_args__ = (
        CheckConstraint(
            "kind IN ('chat_answer','book_updates','new_books')", name="ck_push_pref_kind"
        ),
    )


class PushOutbox(Base):
    __tablename__ = "push_outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict | None] = mapped_column(JSONB)  # custom keys of the payload (type, ids)
    collapse_id: Mapped[str | None] = mapped_column(Text)  # a newer one replaces an older banner
    dedupe_key: Mapped[str | None] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    send_after: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','sent','skipped','failed')", name="ck_push_outbox_status"
        ),
        Index("ix_push_outbox_due", "status", "send_after"),
    )


class PushUserBook(Base):
    """A book the user has downloaded on a device (uploaded by the app), so an update of it can be
    told to them even when it is not in their synced data."""

    __tablename__ = "push_user_books"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    book_id: Mapped[int] = mapped_column(Integer, primary_key=True)

    __table_args__ = (Index("ix_push_user_books_book", "book_id"),)


class PushBookVersion(Base):
    """The content version of each published book as last announced (or seen at the start).
    A book not here is new; a higher content_version in `books` is an update."""

    __tablename__ = "push_book_versions"

    # No foreign key: the row of book 0 marks that the library was recorded at the start.
    book_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_version: Mapped[int] = mapped_column(Integer, nullable=False)
    first_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
