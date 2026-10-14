# 0001. Enums como VARCHAR + CHECK, no enums nativos de Postgres

- Estado: aceptado
- Fecha: 2026-10-14

## Contexto

`CLAUDE.md` pide decidir por ADR entre `TEXT + CHECK` y enums nativos de Postgres. Los primeros enums son `sex`, `activity_level` y `unit_system` (`user_profiles`). Vendrán más (`goal_type`, `meal_type`, `status`...).

## Decisión

Columnas `VARCHAR(32)` con un `CHECK (col IN (...))` con nombre, generadas con el helper `app.core.db.text_enum(EnumCls, name)`. Por dentro es `sa.Enum(native_enum=False, create_constraint=True)`. En Python, los valores son `StrEnum` definidos en el `domain.py` del módulo dueño.

El nombre del CHECK sigue la naming convention: `ck_<tabla>_<name>`.

## Por qué

- Agregar un valor a un enum nativo requiere `ALTER TYPE ... ADD VALUE`, que no puede correr dentro de una transacción en todos los casos. Quitar un valor requiere recrear el tipo. Con un CHECK basta con `DROP CONSTRAINT` + `ADD CONSTRAINT` en una migración normal.
- Alembic autogenerate no maneja bien los cambios en enums nativos.
- No hay diferencia de rendimiento relevante a esta escala.

## Consecuencias

- **Alembic no detecta cambios en los valores permitidos de un CHECK.** Al agregar o quitar un valor del `StrEnum` hay que escribir la migración a mano: drop + create del constraint `ck_<tabla>_<name>`.
- **Autogenerate duplica los CHECK** al crear una tabla con `text_enum`: renderiza el `sa.Enum` y además los CHECK explícitos. Al revisar la migración, deja la columna como `sa.String(length=32)` y conserva solo los CHECK con `op.f("ck_...")`. Ver `alembic/versions/*_users.py`.
- `tests/integration/test_migrations.py` verifica que modelos y migraciones no diverjan.
