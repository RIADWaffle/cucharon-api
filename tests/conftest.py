"""Fixtures compartidos.

Los fixtures de DB son perezosos: los tests unit no los piden y no levantan Docker.
- `postgres_url` (sesión): un Postgres por corrida de pytest (testcontainers).
- `engine` (sesión): aplica las migraciones de Alembic (así también se prueban).
- `db_connection` (test): transacción externa que se revierte al final del test.
- `db_session` / `app`: sesiones con `join_transaction_mode="create_savepoint"`, así
  los commits de `get_session` liberan un savepoint y nada sale del test.
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

# `app.main` crea la app al importarse y lee la config del entorno.
os.environ.setdefault("DATABASE_URL", "postgresql://unused:unused@localhost:1/unused")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("REFRESH_DERIVATION_KEY", "test-refresh-key")

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.community.postgres import PostgresContainer

from app.core.config import Settings
from app.core.db import create_sessionmaker
from app.core.rate_limit import limiter
from app.main import create_app
from app.providers.food_data import get_food_data_provider
from app.providers.llm import get_llm_provider
from app.providers.storage import get_storage_provider
from tests.fakes.food_data import FakeFoodDataProvider
from tests.fakes.llm import FakeLLMProvider
from tests.fakes.storage import FakeStorageProvider
from tests.support import IsolatedSettings

ROOT = Path(__file__).resolve().parents[1]
POSTGRES_IMAGE = "postgres:17-alpine"


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    return IsolatedSettings(
        env="test",
        log_json=False,
        database_url="postgresql://unused:unused@localhost:1/unused",
        jwt_secret=SecretStr("test-secret-0123456789abcdef0123456789abcdef"),
        refresh_derivation_key=SecretStr("test-refresh-key-0123456789abcdef01234567"),
        storage_local_secret=SecretStr("test-storage-secret"),
    )


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    with PostgresContainer(POSTGRES_IMAGE, driver="asyncpg") as pg:
        yield pg.get_connection_url()


def _run_migrations(connection: Connection) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.attributes["connection"] = connection
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
async def engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(postgres_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(_run_migrations)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:
            yield conn
        finally:
            await transaction.rollback()


@pytest.fixture
async def db_session(db_connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    session = AsyncSession(
        bind=db_connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    try:
        yield session
    finally:
        await session.close()


@pytest.fixture
def fake_llm() -> FakeLLMProvider:
    return FakeLLMProvider()


@pytest.fixture
def fake_food_data() -> FakeFoodDataProvider:
    return FakeFoodDataProvider()


@pytest.fixture
def fake_storage() -> FakeStorageProvider:
    return FakeStorageProvider()


@pytest.fixture
def app(
    test_settings: Settings,
    db_connection: AsyncConnection,
    fake_llm: FakeLLMProvider,
    fake_food_data: FakeFoodDataProvider,
    fake_storage: FakeStorageProvider,
) -> FastAPI:
    app = create_app(test_settings)
    # httpx.ASGITransport no corre el lifespan: el sessionmaker se cablea aquí.
    app.state.sessionmaker = create_sessionmaker(
        db_connection, join_transaction_mode="create_savepoint"
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake_llm
    app.dependency_overrides[get_food_data_provider] = lambda: fake_food_data
    app.dependency_overrides[get_storage_provider] = lambda: fake_storage
    limiter.reset()
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
