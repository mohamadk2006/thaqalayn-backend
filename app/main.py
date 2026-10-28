"""FastAPI application factory.

Routers are mounted under /api so that Nginx on the VPS can proxy a single prefix, and
so the eventual static/file routes never collide with the JSON API.
"""

import asyncio
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

from app import __version__
from app.api import (
    account,
    admin,
    books,
    catalog_sync,
    chat,
    conversations,
    errors,
    health,
    metadata,
    search,
    sync,
    toc_search,
    works,
)
from app.config import get_settings
from app.db import dispose_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    logging.getLogger(__name__).info(
        "starting thaqalayn-backend %s (books_root=%s, covers_root=%s)",
        __version__,
        settings.books_root,
        settings.covers_root,
    )
    cleaner = asyncio.create_task(_daily_cleanup())
    yield
    cleaner.cancel()
    await dispose_engine()


async def _daily_cleanup() -> None:
    """Old sign-in codes, rate-limit rows, email send logs and ended logins, once a day."""
    from app.db import get_sessionmaker
    from app.services import auth_service, chat_history

    while True:
        try:
            async with get_sessionmaker()() as session:
                await auth_service.cleanup(session)
                await chat_history.purge(session)
        except Exception:  # noqa: BLE001 -- a failed cleanup is retried tomorrow
            logging.getLogger(__name__).exception("daily account cleanup failed")
        await asyncio.sleep(24 * 3600)


class _HideSearchText(logging.Filter):
    """The server's request log must not hold what users search for: the value of `q` in an
    access-log line (search, meaning search, titles, authors, contents) is replaced."""

    _Q = re.compile(r"([?&]q=)[^&\s]*")

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], self._Q.sub(r"\1…", args[2]), *args[3:])
        return True


def create_app() -> FastAPI:
    access_log = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _HideSearchText) for f in access_log.filters):
        access_log.addFilter(_HideSearchText())
    app = FastAPI(
        title="Thaqalayn Library API",
        version=__version__,
        description="Catalog, download, and Arabic full-text search for the Thaqalayn library.",
        lifespan=lifespan,
    )
    # Gzip transport for responses over 1KB — book downloads are the target (Arabic
    # text compresses 70-80%+), but this also shrinks large catalog/search pages for
    # free. TEMPORARY: when Nginx goes in front on the VPS (the HTTPS/Nginx
    # milestone), decide whether Nginx's own `gzip on` replaces this middleware or
    # whether both stay — double compression isn't broken (gzip won't recompress an
    # already-gzip Content-Encoding), just pointless CPU. Remove this if Nginx takes
    # over the job.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # HTTPS only from now on for browsers that have seen the API once (accounts send tokens).
    @app.middleware("http")
    async def _hsts(request, call_next):
        response = await call_next(request)
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    # Lets the public website (its own domain) call the API from the browser: reading the
    # library, and signing in, syncing and chatting (POST/PATCH/DELETE with a bearer token
    # in the Authorization header). Without cookies or credentials: /admin's HTTP Basic
    # login is never sent cross-site. ETag is exposed so the site can use
    # /api/catalog/version's If-None-Match the way the app does; Retry-After so it can
    # read how long a 429 asks it to wait.
    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?"
        if settings.cors_allow_localhost
        else None,
        allow_methods=["GET", "HEAD", "POST", "PATCH", "DELETE"],
        allow_headers=["*"],
        expose_headers=["ETag", "Retry-After"],
        allow_credentials=False,
        max_age=86400,
    )

    app.include_router(health.router, prefix="/api")
    app.include_router(works.router, prefix="/api")
    app.include_router(catalog_sync.router, prefix="/api")  # before books: /books/changes
    app.include_router(books.router, prefix="/api")
    app.include_router(metadata.router, prefix="/api")
    app.include_router(search.router, prefix="/api")
    app.include_router(toc_search.router, prefix="/api")
    app.include_router(chat.router, prefix="/api")
    app.include_router(conversations.router, prefix="/api")
    app.include_router(account.router, prefix="/api")
    app.include_router(sync.router, prefix="/api")
    errors.register(app)
    app.include_router(admin.router)
    return app


app = create_app()
