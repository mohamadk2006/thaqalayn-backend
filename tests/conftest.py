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
