"""Async SQLAlchemy engine and session wiring."""

import os
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for every ORM model (schema arrives in Milestone 3)."""


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        # Pool size is only ever overridden by the bulk importer (via these env vars,
        # set before it calls get_sessionmaker() — see scripts/import/import_books.py),
        # never by the API process, so this can't change the live API's connection
        # budget. SQLAlchemy's defaults (5 + 10 overflow = 15) otherwise apply.
        pool_kwargs = {}
        if pool_size := os.environ.get("DB_POOL_SIZE"):
            pool_kwargs["pool_size"] = int(pool_size)
        if max_overflow := os.environ.get("DB_MAX_OVERFLOW"):
            pool_kwargs["max_overflow"] = int(max_overflow)
        # pool_pre_ping guards against connections silently killed by a restarted
        # container or a VPS network blip — cheap insurance for a long-running API.
        _engine = create_async_engine(
            settings.database_url, pool_pre_ping=True, future=True, **pool_kwargs
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session scoped to one request."""
    async with get_sessionmaker()() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
