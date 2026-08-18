"""FastAPI application factory.

Routers are mounted under /api so that Nginx on the VPS can proxy a single prefix, and
so the eventual static/file routes never collide with the JSON API.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api import books, health, metadata, works
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
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Thaqalayn Library API",
        version=__version__,
        description="Catalog, download, and Arabic full-text search for the Thaqalayn library.",
        lifespan=lifespan,
    )
    app.include_router(health.router, prefix="/api")
    app.include_router(works.router, prefix="/api")
    app.include_router(books.router, prefix="/api")
    app.include_router(metadata.router, prefix="/api")
    return app


app = create_app()
