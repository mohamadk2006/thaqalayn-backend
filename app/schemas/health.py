from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Response for GET /api/health.

    `status` is "ok" only when every dependency the API needs to serve real traffic is
    actually reachable — a health check that returns 200 while the database is down is
    worse than no health check, because it's what a VPS load balancer or an uptime
    monitor will trust.
    """

    status: Literal["ok", "degraded"]
    version: str
    database: Literal["up", "down"]
    books_root_present: bool
    covers_root_present: bool
