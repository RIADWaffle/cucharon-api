import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.db import Base
from app.modules import models_registry  # noqa: F401  # pyright: ignore[reportUnusedImport]


def _diff(connection: Connection) -> list[object]:
    context = MigrationContext.configure(
        connection, opts={"compare_type": True, "compare_server_default": True}
    )
    return list(compare_metadata(context, Base.metadata))


# Alembic no compara expresiones de columnas generadas (Postgres reescribe el texto) y
# avisa en cada una; no es una diferencia real. Ver ADR 0006.
@pytest.mark.filterwarnings("ignore:Computed default on .* cannot be modified:UserWarning")
async def test_migrations_match_models(db_connection: AsyncConnection) -> None:
    """Si falla: cambiaste un modelo sin generar su migración (o al revés)."""
    assert await db_connection.run_sync(_diff) == []
