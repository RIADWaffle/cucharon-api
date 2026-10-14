# CLAUDE.md

App de recetas y conteo de calorías. **Multiusuario**. En móvil es **offline-first**.

Antes de tocar cualquier módulo, lee `docs/MODULES.md`. Ahí está qué hace cada módulo, sus tablas y sus fronteras.

## Stack

- **API**: Python 3.12+, FastAPI, SQLAlchemy 2.0 (async) + asyncpg, Pydantic v2, pydantic-settings, Alembic
- **DB**: PostgreSQL (Neon). **Archivos**: Cloudflare R2 (S3 compatible)
- **Auth**: JWT de acceso (corto) + refresh token rotativo guardado hasheado. Passwords con argon2 (`pwdlib`)
- **Cliente**: app móvil Expo + WatermelonDB en **repo aparte**. Este repo es solo la API.
- **Tooling**: uv, ruff (lint + format), pyright, pytest + pytest-asyncio, httpx, testcontainers

## Estructura

```
app/
  main.py
  core/                   # config, db, security, errors, logging
  modules/
    users/ products/ recipes/ metrics/ sync/
  providers/
    llm/ food_data/ storage/
alembic/
tests/
  unit/ integration/ e2e/ smoke/ contract/
  fakes/                  # fakes de providers
docs/
  MODULES.md
  decisions/              # ADRs: 0001-titulo.md
  test-vectors/           # casos JSON de escalado/nutrición; fuente de verdad para el cliente
openapi.json              # contrato con el cliente, generado y commiteado
```

Cada módulo tiene esta estructura:

```
modules/<name>/
  __init__.py       # API pública del módulo (lo único que otros módulos pueden importar)
  router.py         # solo HTTP
  schemas.py        # Pydantic request/response
  models.py         # SQLAlchemy ORM
  service.py        # lógica de negocio, recibe AsyncSession
  domain.py         # funciones puras, sin IO
  exceptions.py     # excepciones de dominio
  dependencies.py   # Depends específicos del módulo (si aplica)
```

## Reglas de arquitectura

1. **Monolito modular por feature.** No hay carpetas globales de `services/` ni `routers/`.
2. **El router es delgado**: valida, llama al service y regresa un schema. No pongas lógica de negocio en el router.
3. **El service recibe la `AsyncSession`.** No uses un repository genérico (`BaseRepository[T]`). Si una query se repite, sácala a una función dentro del mismo módulo.
4. **La lógica pura va en `domain.py`**: cálculos, escalado, conversiones, TDEE. Nada de DB, red ni `datetime.now()` ahí (el tiempo se recibe como parámetro).
5. **Fronteras entre módulos**: un módulo solo importa lo que otro expone en su `__init__.py`. **Prohibido importar `models` de otro módulo** o hacer queries a sus tablas. Las FKs entre módulos se declaran por nombre de tabla (`ForeignKey("products.id")`).
6. **Providers desacoplados**: los módulos dependen del `Protocol` en `providers/<x>/base.py`, nunca de una implementación concreta. Los SDKs externos (`openai`, `aioboto3`, `httpx` hacia APIs de terceros) solo se importan dentro de `providers/<x>/`. La implementación se elige por config en `providers/<x>/factory.py` y se inyecta con `Depends`.
7. **`user_id` siempre sale de `get_current_user`**, nunca del body ni del query string. Toda query sobre datos de usuario filtra por `user_id`.
8. **Los services no conocen HTTP.** Lanzan excepciones de dominio (`exceptions.py`) y `core/errors.py` las convierte a respuestas HTTP.
9. **El LLM nunca calcula números.** Calorías, macros, TDEE y metas salen de código determinista o de la DB. El LLM solo parsea texto a estructuras y redacta explicaciones.

## Convenciones de datos

- PK `UUID`. **Se aceptan IDs generados por el cliente** (requisito del sync offline).
- Toda tabla de usuario tiene `id, user_id, created_at, updated_at, deleted_at`. `updated_at` lo pone **el servidor**.
- **Soft delete siempre** en tablas sincronizables.
- Cantidades en `Numeric` / `Decimal`, nunca float. Unidad base: gramos o ml. La nutrición se guarda **por 100 g**.
- Fechas en `timestamptz` UTC. El "día" del usuario se calcula con la `timezone` de su perfil, nunca con la del servidor.
- Los registros de comida guardan un **snapshot** nutricional. Editar una receta o producto no reescribe el historial.
- Tablas en plural y `snake_case`. Enums como `TEXT` + `CHECK` o enum de Postgres, decidido por ADR.

## DB / async: reglas para evitar bugs

- Las relaciones se declaran con `lazy="raise"` y se cargan con `selectinload` explícito. Lazy loading en async truena.
- Un commit por request dentro de `get_session`, con rollback si hay excepción. Los services no hacen commit, solo `flush` cuando necesitan IDs.
- No llames código bloqueante dentro de rutas async (`requests`, SDKs sync, `time.sleep`).
- Usa `alembic revision --autogenerate` y **revisa la migración antes de aplicarla**. Nunca edites una migración que ya se aplicó.

## Testing (obligatorio)

| Nivel | Carpeta | Qué prueba | IO |
|---|---|---|---|
| unit | `tests/unit` | `domain.py` y services con fakes | ninguno |
| integration | `tests/integration` | services + Postgres real (testcontainers), rollback por test | DB |
| e2e | `tests/e2e` | flujos HTTP completos con `httpx.AsyncClient` | DB |
| smoke | `tests/smoke` | `SMOKE_BASE_URL` desplegado: health, auth, lectura | red |
| contract | `tests/contract` | providers reales (`@pytest.mark.external`) | red |

Reglas:
- Todo feature entra con tests. Todo bugfix empieza con un test que lo reproduce.
- Unit, integration y e2e usan **fakes** de providers (`tests/fakes/`) inyectados con `app.dependency_overrides`. Nunca le pegan a Groq, Open Food Facts ni R2.
- No se mockea SQLAlchemy. Si necesitas DB, usa un integration test.
- `domain.py` va cubierto en todas sus ramas. En el resto no se persigue un porcentaje de coverage.
- Las fórmulas de escalado y nutrición se prueban contra `docs/test-vectors/*.json`. Esos archivos son la fuente de verdad que también consume el cliente; cambiarlos es un cambio de contrato.
- `openapi.json` se regenera con cada cambio de API. CI falla si el archivo commiteado no coincide con el generado.
- Smoke: timeout de 90 s en el primer request (cold start de Render free). Solo lectura y usuario de prueba dedicado.
- Contract tests no corren en CI por default.

## Comandos

```bash
uv run uvicorn app.main:app --reload
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run pytest -m "not external" tests/unit tests/integration tests/e2e
uv run pytest tests/smoke              # requiere SMOKE_BASE_URL
uv run pytest -m external tests/contract
uv run alembic upgrade head
```

## Cómo trabajamos

- Para cualquier tarea no trivial: primero un **plan corto** y esperas aprobación.
- Cambios pequeños, un módulo a la vez. No refactorices de pasada cosas que no se pidieron.
- No agregues dependencias sin justificarlas en una línea. Si existe una librería estándar para el problema, propónla antes de escribir código propio.
- No inventes APIs de librerías ni versiones. Si no estás seguro, revisa la documentación o dilo.
- Si una regla de este archivo estorba, dilo y propón un ADR. No la rompas en silencio.
- Antes de terminar una tarea, deben pasar ruff, pyright y los tests (excepto `external`).
- Los secretos van solo en `.env` (está en `.gitignore`). Mantén `.env.example` actualizado.
- **Nunca hagas `git commit`, `git push` ni modifiques el historial** (rebase, reset, amend). Los commits los hace el dev. Al terminar una tarea, propón cómo agrupar los cambios en commits con mensajes en Conventional Commits (`feat(recipes): ...`).