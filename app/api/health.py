import logging

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.schemas.health import HealthResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(
    response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HealthResponse:
    """Liveness + dependency check.

    Returns 503 when the database is unreachable so that an uptime monitor, and later
    Nginx, can tell "process is running" apart from "process can actually serve requests".
    """
    try:
        await session.execute(text("SELECT 1"))
        database: str = "up"
    except Exception:
        # Logged with the traceback but never surfaced in the response: connection errors
        # carry the DSN, and the DSN carries the password.
        logger.exception("health check: database unreachable")
        database = "down"

    if database == "down":
        response.status_code = 503

    return HealthResponse(
        status="ok" if database == "up" else "degraded",
        version=settings_version(),
        database=database,  # type: ignore[arg-type]
        books_root_present=settings.books_root.is_dir(),
        covers_root_present=settings.covers_root.is_dir(),
    )


def settings_version() -> str:
    from app import __version__

    return __version__
