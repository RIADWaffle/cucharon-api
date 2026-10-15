from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.db import Base
from app.modules import models_registry  # noqa: F401  # pyright: ignore[reportUnusedImport]

ROOT = Path(__file__).resolve().parents[2]


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


def _stairway(connection: Connection) -> list[str]:
    """Baja a base y, revisión por revisión: upgrade, downgrade -1, upgrade.

    Corre dentro de la transacción del test (Alembic no commitea en una transacción
    externa y el DDL de Postgres es transaccional): al final todo se revierte.
    """
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.attributes["connection"] = connection
    config.attributes["configure_logger"] = False
    revisions = [r.revision for r in ScriptDirectory.from_config(config).walk_revisions()]
    revisions.reverse()  # de la primera a head

    command.downgrade(config, "base")
    for revision in revisions:
        command.upgrade(config, revision)
        command.downgrade(config, "-1")
        command.upgrade(config, revision)
    return revisions


@pytest.mark.filterwarnings("ignore:Computed default on .* cannot be modified:UserWarning")
async def test_every_migration_downgrades_and_upgrades(db_connection: AsyncConnection) -> None:
    """Si falla: el downgrade de alguna revisión no deshace bien su upgrade."""
    revisions = await db_connection.run_sync(_stairway)

    assert len(revisions) >= 3
    current = await db_connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert current == revisions[-1]
    assert await db_connection.run_sync(_diff) == []


async def test_products_has_a_single_unique_index_besides_pk(
    db_connection: AsyncConnection,
) -> None:
    rows = await db_connection.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename = 'products' "
            "AND indexdef LIKE 'CREATE UNIQUE INDEX%'"
        )
    )
    unique_indexes = {name for (name,) in rows}
    assert unique_indexes == {"pk_products", "uq_products_source_external_id"}, (
        f"Índices únicos en products: {sorted(unique_indexes)}. Los upserts usan ON CONFLICT "
        "sobre uq_products_source_external_id, que solo arbitra ese índice: otro índice "
        "único hace que escaneos concurrentes truenen con UniqueViolationError. "
        "Ver ADR 0006 (D5)."
    )
