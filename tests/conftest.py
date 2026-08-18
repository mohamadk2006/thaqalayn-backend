"""Shared test fixtures.

`app.db` caches one engine per process on purpose — a long-running API has exactly one
event loop, and rebuilding the pool per request would be wasteful. Tests break that
assumption: pytest-asyncio runs each test in its own event loop, so an engine created in
one test's loop leaves asyncpg holding pooled connections bound to a loop that no longer
exists, and the next teardown that touches them blocks forever.

Disposing between tests keeps the production caching behaviour intact while giving each
test a clean engine bound to its own loop.
"""

import pytest

from app.db import dispose_engine


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    yield
    await dispose_engine()


@pytest.fixture
async def session():
    """A session whose work is always rolled back.

    Schema tests write real rows — works, books, pages — and must not leave them behind
    for the next test or for whoever is looking at the development database. Binding the
    session to an outer transaction that is unconditionally rolled back gives each test a
    clean database without the cost of recreating the schema per test.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db import get_engine

    connection = await get_engine().connect()
    transaction = await connection.begin()
    # join_transaction_mode="create_savepoint" makes the session operate inside a
    # SAVEPOINT rather than the outer transaction directly. Without it, a test that
    # deliberately triggers an IntegrityError and rolls back would unwind the fixture's
    # own transaction, leaving teardown to warn about a deassociated transaction.
    async_session = AsyncSession(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield async_session
    finally:
        await async_session.close()
        await transaction.rollback()
        await connection.close()
