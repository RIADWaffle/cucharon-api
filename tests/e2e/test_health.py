import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.db import create_sessionmaker


async def test_health_is_liveness_only(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "x-request-id" in response.headers


async def test_ready_pings_db(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}


async def test_ready_returns_503_when_db_is_down(app: FastAPI, client: httpx.AsyncClient) -> None:
    dead = create_async_engine("postgresql+asyncpg://x:x@127.0.0.1:1/x", poolclass=NullPool)
    app.state.sessionmaker = create_sessionmaker(dead)
    try:
        response = await client.get("/health/ready")
    finally:
        await dead.dispose()
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "db": "error"}


async def test_unknown_route_uses_error_format(client: httpx.AsyncClient) -> None:
    response = await client.get("/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "http_error"
