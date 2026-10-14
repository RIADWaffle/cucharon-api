"""`get_session` hace commit ANTES de mandar la respuesta (`scope="function"`).

Si el commit falla, el cliente debe recibir 500, no el 2xx que ya había calculado
el endpoint. Este test usa commits reales (no el savepoint del fixture `app`):
un FK `DEFERRABLE INITIALLY DEFERRED` solo se valida en el COMMIT.
"""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.core.db import SessionDep, create_sessionmaker
from app.main import create_app


@pytest.fixture
def commit_app(test_settings: Settings, engine: AsyncEngine) -> FastAPI:
    app = create_app(test_settings)
    app.state.sessionmaker = create_sessionmaker(engine)

    @app.post("/_test/write")
    async def write(session: SessionDep, violate: bool) -> dict[str, str]:
        # Tabla temporal que se borra con la transacción: no queda nada en la DB.
        await session.execute(
            text(
                "CREATE TEMP TABLE commit_probe ("
                " id int PRIMARY KEY,"
                " parent int REFERENCES commit_probe (id) DEFERRABLE INITIALLY DEFERRED"
                ") ON COMMIT DROP"
            )
        )
        parent = 999 if violate else 1
        await session.execute(
            text("INSERT INTO commit_probe VALUES (1, :parent)"), {"parent": parent}
        )
        return {"status": "ok"}

    return app


@pytest.fixture
async def commit_client(commit_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    # Starlette re-lanza la excepción después de mandar el 500; aquí solo interesa
    # la respuesta que vio el cliente.
    transport = httpx.ASGITransport(app=commit_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_successful_commit_returns_2xx(commit_client: httpx.AsyncClient) -> None:
    response = await commit_client.post("/_test/write", params={"violate": False})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_failed_commit_returns_500_not_2xx(commit_client: httpx.AsyncClient) -> None:
    response = await commit_client.post("/_test/write", params={"violate": True})
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
