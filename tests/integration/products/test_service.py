import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import QueuePool

from app.core.config import Settings
from app.modules.products import service
from app.modules.products.domain import FetchOrigin, NutrientBasis, ProductSource
from app.modules.products.exceptions import (
    BarcodeTakenError,
    CustomProductIdTakenError,
    CustomProductNotFoundError,
    FoodDataUnavailableError,
    InvalidBarcodeError,
    OverrideExistsError,
    ProductNotFoundError,
)
from app.modules.products.models import Product, ProductBarcodeMiss, UserProduct
from app.modules.users.models import User
from app.providers.errors import ProviderUnavailableError
from app.providers.food_data import Nutrients, ProductData, SearchResult
from tests.fakes.food_data import FakeFoodDataProvider

D = Decimal
NUTELLA = "3017620422003"
NUTELLA_GTIN = "03017620422003"


NUTELLA_NUTRIENTS = Nutrients(
    kcal=D(539),
    protein_g=D("6.3"),
    carbs_g=D("57.5"),
    fat_g=D("30.9"),
    fiber_g=D(0),
    sugar_g=D("56.3"),
    sodium_mg=D("42.8"),
)
AVENA_NUTRIENTS = Nutrients(
    kcal=D(389), protein_g=D("16.9"), carbs_g=D(56), fat_g=D("6.9"), fiber_g=D("10.6")
)


def _off(
    code: str,
    name: str,
    *,
    brand: str | None = None,
    nutrients: Nutrients | None = None,
    **changes: Any,
) -> ProductData:
    """Producto de OFF genérico: sin marca y con nutrientes desconocidos salvo que se pasen."""
    fields: dict[str, Any] = {
        "source": "off",
        "external_id": code,
        "name": name,
        "basis": "per_100g",
        "nutrients": nutrients or Nutrients(),
        "barcode": code,
        "brand": brand,
        "source_url": f"https://world.openfoodfacts.org/product/{code}",
    }
    return ProductData(**{**fields, **changes})


def _nutella(*, complete: bool = True, name: str = "Nutella") -> ProductData:
    nutrients = NUTELLA_NUTRIENTS if complete else Nutrients(kcal=D(539))
    return _off(NUTELLA, name, brand="Ferrero", nutrients=nutrients)


def _coca_cola(code: str = "5449000000996") -> ProductData:
    return _off(
        code,
        "Coca-Cola",
        brand="Coca-Cola",
        basis="per_100ml",
        nutrients=Nutrients(
            kcal=D("42.4"), protein_g=D(0), carbs_g=D("10.6"), fat_g=D(0), sugar_g=D("10.6")
        ),
    )


def _with_check_digit(body: str) -> str:
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return body + str((10 - total % 10) % 10)


def _usda(fdc_id: str, name: str, *, aliases: tuple[str, ...] = ()) -> ProductData:
    return ProductData(
        source="usda",
        external_id=fdc_id,
        name=name,
        basis="per_100g",
        source_type="SR Legacy",
        nutrients=Nutrients(kcal=D(89), protein_g=D("1.1"), carbs_g=D("20.2"), fat_g=D("0.3")),
        search_aliases=aliases,
    )


async def _user(session: AsyncSession) -> UUID:
    user = User(email=f"{uuid4().hex}@example.com", password_hash="x")
    session.add(user)
    await session.flush()
    return user.id


async def _lookup(
    session: AsyncSession,
    provider: FakeFoodDataProvider,
    user_id: UUID,
    settings: Settings,
    code: str = NUTELLA,
    now: datetime | None = None,
) -> service.ProductView | None:
    """Como una request: lo anterior ya se commiteó y `get_session` commitea al final."""
    await session.commit()
    view = await service.lookup_barcode(
        session, provider, user_id=user_id, code=code, settings=settings, now=now
    )
    await session.commit()
    return view


async def _lookup_found(
    session: AsyncSession,
    provider: FakeFoodDataProvider,
    user_id: UUID,
    settings: Settings,
    code: str = NUTELLA,
    now: datetime | None = None,
) -> service.ProductView:
    view = await _lookup(session, provider, user_id, settings, code, now)
    assert view is not None
    return view


async def _search(
    session: AsyncSession,
    provider: FakeFoodDataProvider,
    user_id: UUID,
    query: str,
    limit: int = 5,
) -> service.SearchView:
    await session.commit()
    result = await service.search(session, provider, user_id=user_id, query=query, limit=limit)
    await session.commit()
    return result


# --- escaneo ---


async def test_barcode_lookup_caches_normalized(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    user_id = await _user(db_session)
    provider = FakeFoodDataProvider(products=[_nutella()])

    view = await _lookup_found(db_session, provider, user_id, test_settings)
    again = await _lookup_found(db_session, provider, user_id, test_settings, code="03017620422003")

    assert view.kind == "product"
    assert view.barcode == NUTELLA_GTIN
    assert view.nutrition_complete
    assert view.attribution is not None
    assert view.attribution.url.endswith(NUTELLA)
    assert again.id == view.id
    assert provider.barcode_lookups == [NUTELLA_GTIN]  # el segundo salió de la caché
    product = await db_session.get(Product, view.id)
    assert product is not None
    assert product.external_id == NUTELLA_GTIN
    assert product.fetched_via is FetchOrigin.BARCODE


async def test_invalid_barcode_never_reaches_provider(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    provider = FakeFoodDataProvider()
    with pytest.raises(InvalidBarcodeError):
        await _lookup(db_session, provider, uuid4(), test_settings, code="3017620422004")
    assert provider.barcode_lookups == []


async def test_negative_cache(db_session: AsyncSession, test_settings: Settings) -> None:
    user_id = await _user(db_session)
    provider = FakeFoodDataProvider()
    now = datetime.now(UTC)

    assert await _lookup(db_session, provider, user_id, test_settings, now=now) is None
    assert (
        await _lookup(db_session, provider, user_id, test_settings, now=now + timedelta(hours=1))
        is None
    )
    assert len(provider.barcode_lookups) == 1
    assert await db_session.get(ProductBarcodeMiss, NUTELLA_GTIN) is not None

    # Vencida la caché negativa, se vuelve a preguntar (y ya existe).
    provider.products.append(_nutella())
    later = now + timedelta(hours=test_settings.products_miss_ttl_hours)
    view = await _lookup_found(db_session, provider, user_id, test_settings, now=later)
    assert view.name == "Nutella"
    assert len(provider.barcode_lookups) == 2


async def test_unavailable_without_cache_is_503_and_not_a_miss(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    user_id = await _user(db_session)
    provider = FakeFoodDataProvider(
        barcode_error=ProviderUnavailableError("off", "timeout", retry_after=42)
    )
    with pytest.raises(FoodDataUnavailableError) as exc_info:
        await _lookup(db_session, provider, user_id, test_settings)
    assert exc_info.value.retry_after == 42
    assert await db_session.get(ProductBarcodeMiss, NUTELLA_GTIN) is None


async def test_stale_product_is_served_when_provider_fails_and_refreshed_when_back(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    user_id = await _user(db_session)
    old = datetime.now(UTC) - timedelta(days=test_settings.products_cache_ttl_days + 1)
    provider = FakeFoodDataProvider(products=[_nutella()])
    first = await _lookup_found(db_session, provider, user_id, test_settings, now=old)

    provider.barcode_error = ProviderUnavailableError("off", "HTTP 503")
    stale = await _lookup_found(db_session, provider, user_id, test_settings)
    assert stale.stale
    assert stale.id == first.id

    provider.barcode_error = None
    provider.products[0] = _nutella(name="Nutella 2026")
    fresh = await _lookup_found(db_session, provider, user_id, test_settings)
    assert not fresh.stale
    assert fresh.id == first.id
    assert fresh.name == "Nutella 2026"


async def test_product_gone_from_source_keeps_serving_cache(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    user_id = await _user(db_session)
    old = datetime.now(UTC) - timedelta(days=test_settings.products_cache_ttl_days + 1)
    provider = FakeFoodDataProvider(products=[_nutella()])
    first = await _lookup_found(db_session, provider, user_id, test_settings, now=old)

    provider.products.clear()
    view = await _lookup_found(db_session, provider, user_id, test_settings)

    assert view.id == first.id
    assert await db_session.get(ProductBarcodeMiss, NUTELLA_GTIN) is None
    product = await db_session.get(Product, first.id)
    assert product is not None
    assert product.fetched_at > old


async def test_user_override_wins_only_for_its_owner(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    owner = await _user(db_session)
    other = await _user(db_session)
    provider = FakeFoodDataProvider(products=[_nutella()])
    product = await _lookup_found(db_session, provider, owner, test_settings)

    override = await service.create_custom(
        db_session, owner, {"base_product_id": product.id, "nutrients": {"kcal": D(540)}}
    )

    mine = await _lookup_found(db_session, provider, owner, test_settings)
    theirs = await _lookup_found(db_session, provider, other, test_settings)
    assert mine.kind == "user_product"
    assert mine.id == override.id
    assert mine.nutrients.kcal == D(540)
    assert mine.nutrients.protein_g == D("6.3")  # copiado del producto base
    assert mine.attribution is not None  # deriva de OFF: lleva atribución
    assert theirs.kind == "product"


async def test_custom_with_barcode_beats_negative_cache(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    user_id = await _user(db_session)
    provider = FakeFoodDataProvider()
    assert await _lookup(db_session, provider, user_id, test_settings, code="7509999888772") is None

    custom = await service.create_custom(
        db_session,
        user_id,
        {"name": "Salsa de la abuela", "basis": NutrientBasis.PER_100G, "barcode": "7509999888772"},
    )
    view = await _lookup_found(db_session, provider, user_id, test_settings, code="7509999888772")
    assert view.id == custom.id
    assert view.barcode == "07509999888772"
    assert len(provider.barcode_lookups) == 1


async def test_db_only_allows_barcodes_on_off_rows(db_session: AsyncSession) -> None:
    db_session.add(
        Product(
            source=ProductSource.USDA,
            external_id="123",
            barcode=NUTELLA_GTIN,
            name="x",
            basis=NutrientBasis.PER_100G,
            fetched_via=FetchOrigin.SEARCH,
            fetched_at=datetime.now(UTC),
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


# --- concurrencia (commits reales, fuera del rollback por test) ---


class _BarrierProvider(FakeFoodDataProvider):
    """Hace que los dos lookups lleguen al INSERT a la vez."""

    def __init__(self, products: list[ProductData], parties: int) -> None:
        super().__init__(products=products)
        self._barrier = asyncio.Barrier(parties)

    async def get_by_barcode(self, gtin: str) -> ProductData | None:
        result = await super().get_by_barcode(gtin)
        await self._barrier.wait()
        return result


@pytest.fixture
async def committed_user(engine: AsyncEngine) -> AsyncIterator[UUID]:
    """Usuario commiteado de verdad; al final borra lo que commitearon los tests."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        started = await session.scalar(select(func.now()))
        user = User(email=f"{uuid4().hex}@example.com", password_hash="x")
        session.add(user)
        await session.commit()
        user_id = user.id
    yield user_id
    async with AsyncSession(engine) as session:
        await session.execute(delete(Product).where(Product.created_at >= started))
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()


@pytest.mark.slow
async def test_concurrent_lookups_of_same_barcode_do_not_fail(
    pooled_engine: AsyncEngine, committed_user: UUID, test_settings: Settings
) -> None:
    # La carrera ocurre dentro de Postgres (dos inserts pasan el chequeo del árbitro
    # en el mismo instante) y no se puede forzar desde aquí: se repite muchas veces
    # con códigos distintos. Con el índice único viejo en barcode falla siempre.
    codes = [_with_check_digit(f"750{i:09d}") for i in range(200)]
    parallel = 4
    provider = _BarrierProvider(
        [_off(code, f"Producto {code}") for code in codes], parties=parallel
    )

    async def lookup(code: str) -> service.ProductView:
        async with AsyncSession(pooled_engine, expire_on_commit=False) as session:
            return await _lookup_found(session, provider, committed_user, test_settings, code)

    for code in codes:
        views = await asyncio.gather(*(lookup(code) for _ in range(parallel)))
        assert len({view.id for view in views}) == 1

    async with AsyncSession(pooled_engine) as session:
        counts = await session.execute(
            select(Product.barcode, func.count())
            .where(Product.barcode.in_([code.zfill(14) for code in codes]))
            .group_by(Product.barcode)
        )
        assert sorted(count for _, count in counts) == [1] * len(codes)


class _PoolProbe(FakeFoodDataProvider):
    """Anota cuántas conexiones hay tomadas del pool mientras "espera" al provider."""

    def __init__(self, products: list[ProductData], pool: QueuePool) -> None:
        super().__init__(products=products)
        self._pool = pool
        self.checked_out: list[int] = []

    async def get_by_barcode(self, gtin: str) -> ProductData | None:
        self.checked_out.append(self._pool.checkedout())
        return await super().get_by_barcode(gtin)

    async def search(self, query: str, limit: int = 20) -> SearchResult:
        self.checked_out.append(self._pool.checkedout())
        return await super().search(query, limit)


@pytest.fixture
async def pooled_engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(postgres_url, pool_size=8, max_overflow=0)
    yield engine
    await engine.dispose()


async def test_provider_is_called_without_holding_a_db_connection(
    pooled_engine: AsyncEngine, committed_user: UUID, test_settings: Settings
) -> None:
    # Una transacción abierta durante la llamada (hasta 8 s) dejaría la conexión
    # "idle in transaction" y rompería las transacciones cortas del ADR 0004.
    probe = _PoolProbe([_coca_cola()], cast(QueuePool, pooled_engine.pool))
    async with AsyncSession(pooled_engine, expire_on_commit=False) as session:
        await _lookup_found(session, probe, committed_user, test_settings, code="5449000000996")
        result = await _search(session, probe, committed_user, "coca cola", 5)

    assert probe.checked_out == [0, 0]
    assert [item.name for item in result.items] == ["Coca-Cola"]


async def test_writing_before_calling_the_provider_is_a_programming_error(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """El rollback previo a la llamada perdería la escritura: debe tronar (500)."""
    user_id = await _user(db_session)
    await db_session.commit()
    provider = FakeFoodDataProvider(products=[_nutella()])

    # Escritura por flush (ORM).
    await service.create_custom(
        db_session, user_id, {"name": "Salsa", "basis": NutrientBasis.PER_100G}
    )
    with pytest.raises(RuntimeError, match="rollback las perdería"):
        await service.lookup_barcode(
            db_session, provider, user_id=user_id, code=NUTELLA, settings=test_settings
        )
    await db_session.rollback()

    # Escritura por DML directo (no pasa por flush).
    await db_session.execute(update(User).where(User.id == user_id).values(is_active=False))
    with pytest.raises(RuntimeError):
        await service.search(db_session, provider, user_id=user_id, query="nutella", limit=5)
    await db_session.rollback()

    # SQL crudo.
    await db_session.execute(
        text("UPDATE users SET is_active = false WHERE id = :id"), {"id": user_id}
    )
    with pytest.raises(RuntimeError):
        await service.lookup_barcode(
            db_session, provider, user_id=user_id, code=NUTELLA, settings=test_settings
        )
    await db_session.rollback()

    # Cambio pendiente sin flush.
    db_session.add(User(email=f"{uuid4().hex}@example.com", password_hash="x"))
    with pytest.raises(RuntimeError):
        await service.lookup_barcode(
            db_session, provider, user_id=user_id, code=NUTELLA, settings=test_settings
        )
    await db_session.rollback()

    assert provider.barcode_lookups == []
    assert provider.search_queries == []
    # Commiteado (otra request), ya no cuenta como escritura de esta transacción.
    await service.create_custom(
        db_session, user_id, {"name": "Salsa", "basis": NutrientBasis.PER_100G}
    )
    await db_session.commit()
    view = await service.lookup_barcode(
        db_session, provider, user_id=user_id, code=NUTELLA, settings=test_settings
    )
    assert view is not None


# --- búsqueda ---


async def _seed_product(session: AsyncSession, data: ProductData) -> None:
    await service._upsert_from_search(session, [data], datetime.now(UTC))


@pytest.mark.parametrize("query", ["platano", "plátano", "PLÁTANO", "Platano"])
async def test_search_ignores_accents_and_case(db_session: AsyncSession, query: str) -> None:
    user_id = await _user(db_session)
    await _seed_product(db_session, _off("7501234567893", "Plátano Tabasco"))
    result = await _search(db_session, FakeFoodDataProvider(), user_id, query, 5)
    assert "Plátano Tabasco" in [item.name for item in result.items]


async def test_search_finds_user_products_without_accents(db_session: AsyncSession) -> None:
    user_id = await _user(db_session)
    await service.create_custom(
        db_session, user_id, {"name": "Tortilla de maíz", "basis": NutrientBasis.PER_100G}
    )
    result = await _search(db_session, FakeFoodDataProvider(), user_id, "tortilla de maiz", 5)
    assert [item.kind for item in result.items][:1] == ["user_product"]


async def test_search_goes_external_only_when_local_is_not_enough(
    db_session: AsyncSession,
) -> None:
    user_id = await _user(db_session)
    await _seed_product(db_session, _off("7501234567893", "Avena integral"))
    provider = FakeFoodDataProvider(products=[_off("7501234567886", "Avena con miel")])

    enough = await _search(db_session, provider, user_id, "avena", 1)
    assert provider.search_queries == []
    assert [item.name for item in enough.items] == ["Avena integral"]

    more = await _search(db_session, provider, user_id, "avena", 5)
    assert provider.search_queries == ["avena"]
    assert {item.name for item in more.items} == {"Avena integral", "Avena con miel"}
    # Lo externo quedó en caché con id estable.
    cached = await db_session.scalar(select(Product).where(Product.name == "Avena con miel"))
    assert cached is not None
    assert cached.fetched_via is FetchOrigin.SEARCH


async def test_search_is_partial_when_a_source_fails(db_session: AsyncSession) -> None:
    user_id = await _user(db_session)
    await _seed_product(db_session, _off("7501234567893", "Avena integral"))
    down = FakeFoodDataProvider(search_error=ProviderUnavailableError("off", "429"))
    result = await _search(db_session, down, user_id, "avena", 5)
    assert result.partial
    assert [item.name for item in result.items] == ["Avena integral"]

    half = FakeFoodDataProvider(unavailable_sources=frozenset({"usda"}))
    result = await _search(db_session, half, user_id, "avena", 5)
    assert result.partial


async def test_usda_alias_makes_english_result_findable_in_spanish(
    db_session: AsyncSession,
) -> None:
    user_id = await _user(db_session)
    provider = FakeFoodDataProvider(
        products=[_usda("173944", "Bananas, raw", aliases=("platano",))]
    )
    # El fake busca por substring del nombre en inglés; el glosario ya tradujo.
    first = await _search(db_session, provider, user_id, "bananas", 5)
    assert [item.name for item in first.items] == ["Bananas, raw"]

    result = await _search(db_session, FakeFoodDataProvider(), user_id, "plátano", 5)
    assert [item.name for item in result.items] == ["Bananas, raw"]


async def test_search_shows_override_instead_of_base(db_session: AsyncSession) -> None:
    user_id = await _user(db_session)
    await _seed_product(db_session, _off("7501234567893", "Avena integral"))
    base = await db_session.scalar(select(Product).where(Product.name == "Avena integral"))
    assert base is not None
    override = await service.create_custom(
        db_session, user_id, {"base_product_id": base.id, "name": "Avena integral (mía)"}
    )
    result = await _search(db_session, FakeFoodDataProvider(), user_id, "avena integral", 5)
    assert [item.id for item in result.items] == [override.id]


# --- la búsqueda no degrada la caché ---


async def test_search_never_degrades_a_scanned_product(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    user_id = await _user(db_session)
    scanned = await _lookup_found(
        db_session, FakeFoodDataProvider(products=[_nutella()]), user_id, test_settings
    )
    before = await db_session.get(Product, scanned.id)
    assert before is not None
    fetched_at = before.fetched_at

    poorer = FakeFoodDataProvider(products=[_nutella(complete=False, name="Nutella (OFF search)")])
    await _search(db_session, poorer, user_id, "nutella", 5)

    product = await db_session.get(Product, scanned.id)
    assert product is not None
    assert product.name == "Nutella"
    assert product.protein_g == D("6.3")
    assert product.fetched_via is FetchOrigin.BARCODE
    assert product.fetched_at == fetched_at


async def test_search_touches_identical_data(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    user_id = await _user(db_session)
    old = datetime.now(UTC) - timedelta(days=5)
    scanned = await _lookup_found(
        db_session, FakeFoodDataProvider(products=[_nutella()]), user_id, test_settings, now=old
    )
    await service._upsert_from_search(db_session, [_nutella()], datetime.now(UTC))
    product = await db_session.get(Product, scanned.id)
    assert product is not None
    assert product.fetched_at > old
    assert product.fetched_via is FetchOrigin.BARCODE


async def test_search_keeps_richer_search_data_and_fills_poorer(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    await service._upsert_from_search(db_session, [_nutella()], now)
    await service._upsert_from_search(db_session, [_nutella(complete=False)], now)
    product = await db_session.scalar(select(Product).where(Product.external_id == NUTELLA_GTIN))
    assert product is not None
    assert product.protein_g == D("6.3")

    poor = _off("7501234567893", "Avena integral", nutrients=Nutrients(kcal=D(389)))
    rich = _off("7501234567893", "Avena integral", nutrients=AVENA_NUTRIENTS)
    await service._upsert_from_search(db_session, [poor], now)
    await service._upsert_from_search(db_session, [rich], now)
    filled = await db_session.scalar(select(Product).where(Product.external_id == "07501234567893"))
    assert filled is not None
    assert filled.protein_g == D("16.9")


# --- CRUD de custom ---


async def test_custom_crud_and_isolation(db_session: AsyncSession) -> None:
    owner = await _user(db_session)
    other = await _user(db_session)
    client_id = uuid4()
    created = await service.create_custom(
        db_session,
        owner,
        {
            "id": client_id,
            "name": "Licuado verde",
            "basis": NutrientBasis.PER_100ML,
            "nutrients": {"kcal": D(45), "protein_g": D(2), "carbs_g": D(8), "fat_g": D("0.5")},
            "density_g_per_ml": D("1.04"),
        },
    )
    assert created.id == client_id
    assert created.basis is NutrientBasis.PER_100ML
    assert created.nutrition_complete

    updated = await service.update_custom(
        db_session, owner, client_id, {"nutrients": {"kcal": D(50)}, "serving": None}
    )
    assert updated.nutrients.kcal == D(50)
    assert updated.nutrients.protein_g == D(2)

    with pytest.raises(CustomProductNotFoundError):
        await service.get_custom(db_session, other, client_id)
    with pytest.raises(CustomProductIdTakenError):
        await service.create_custom(
            db_session, other, {"id": client_id, "name": "x", "basis": NutrientBasis.PER_100G}
        )

    await service.delete_custom(db_session, owner, client_id)
    with pytest.raises(CustomProductNotFoundError):
        await service.get_custom(db_session, owner, client_id)
    row = await db_session.get(UserProduct, client_id)
    assert row is not None
    assert row.deleted_at is not None  # borrado lógico


async def test_custom_conflicts(db_session: AsyncSession) -> None:
    user_id = await _user(db_session)
    await _seed_product(db_session, _off("7501234567893", "Avena integral"))
    base = await db_session.scalar(select(Product).where(Product.name == "Avena integral"))
    assert base is not None

    await service.create_custom(db_session, user_id, {"base_product_id": base.id})
    with pytest.raises(OverrideExistsError):
        await service.create_custom(db_session, user_id, {"base_product_id": base.id})

    fields = {"name": "Otro", "basis": NutrientBasis.PER_100G, "barcode": "7501234567893"}
    with pytest.raises(BarcodeTakenError):  # el override heredó el código del base
        await service.create_custom(db_session, user_id, fields)
    with pytest.raises(InvalidBarcodeError):
        await service.create_custom(db_session, user_id, {**fields, "barcode": "123"})
    with pytest.raises(ProductNotFoundError):
        await service.create_custom(db_session, user_id, {"base_product_id": uuid4()})


async def test_deleted_override_frees_the_slot(db_session: AsyncSession) -> None:
    user_id = await _user(db_session)
    await _seed_product(db_session, _off("7501234567893", "Avena integral"))
    base = await db_session.scalar(select(Product).where(Product.name == "Avena integral"))
    assert base is not None
    first = await service.create_custom(db_session, user_id, {"base_product_id": base.id})
    await service.delete_custom(db_session, user_id, first.id)
    second = await service.create_custom(db_session, user_id, {"base_product_id": base.id})
    assert second.id != first.id


async def test_get_product(db_session: AsyncSession) -> None:
    await _seed_product(db_session, _usda("173944", "Bananas, raw"))
    banana = await db_session.scalar(select(Product).where(Product.external_id == "173944"))
    assert banana is not None
    view = await service.get_product(db_session, banana.id)
    assert view.source == "usda"
    assert view.attribution is None  # USDA es CC0
    with pytest.raises(ProductNotFoundError):
        await service.get_product(db_session, uuid4())


async def test_out_of_range_values_are_stored_as_unknown(db_session: AsyncSession) -> None:
    absurd = _off("7501234567893", "Error de captura", nutrients=Nutrients(sodium_mg=D("1e12")))
    await _seed_product(db_session, absurd)
    product = await db_session.scalar(
        select(Product).where(Product.external_id == "07501234567893")
    )
    assert product is not None
    assert product.sodium_mg is None
    assert "value_out_of_range" in product.quality_flags
