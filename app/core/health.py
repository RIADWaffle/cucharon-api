import asyncio

import structlog
from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from app.core.db import SessionDep

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["health"])

DB_PING_TIMEOUT_SECONDS = 3


class HealthResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    db: str


@router.get("/health")
async def health() -> HealthResponse:
    """Liveness: no toca la DB (health check de Render)."""
    return HealthResponse(status="ok")


@router.get(
    "/health/ready",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def ready(session: SessionDep, response: Response) -> ReadinessResponse:
    """Readiness: ping a la DB."""
    try:
        async with asyncio.timeout(DB_PING_TIMEOUT_SECONDS):
            await session.execute(text("SELECT 1"))
    except Exception:
        logger.warning("db_ping_failed", exc_info=True)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(status="unavailable", db="error")
    return ReadinessResponse(status="ok", db="ok")
