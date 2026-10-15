"""Lógica de products. Ver ADR 0006 para las decisiones de caché, búsqueda y datos."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

import structlog
from sqlalchemy import ColumnElement, Text, func, literal, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.config import Settings
from app.core.db import has_writes
from app.modules.products.domain import (
    ODBL_URL,
    OFF_ATTRIBUTION_TEXT,
    CacheState,
    FetchOrigin,
    NutrientBasis,
    ProductSnapshot,
    ProductSource,
    SearchCandidate,
    SearchUpsertAction,
    ServingUnit,
    cache_state,
    drop_overridden,
    miss_is_fresh,
    needs_external_search,
    normalize_gtin,
    rank_candidates,
    search_upsert_action,
)
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
from app.providers.errors import ProviderBadDataError, ProviderUnavailableError
from app.providers.food_data import FoodDataProvider, ProductData, QualityFlag

logger = structlog.get_logger(__name__)

NUTRIENT_FIELDS = ("kcal", "protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g", "sodium_mg")
_QUANTUM = Decimal("0.001")
_MAX_NUMERIC = Decimal(10_000_000)  # Numeric(10, 3)
_PROVIDER_FAILURES = (ProviderUnavailableError, ProviderBadDataError)


# --- vistas que regresa el service (el router las convierte a schemas) ---


@dataclass(frozen=True)
class NutrientValues:
    kcal: Decimal | None
    protein_g: Decimal | None
    carbs_g: Decimal | None
    fat_g: Decimal | None
    fiber_g: Decimal | None
    sugar_g: Decimal | None
    sodium_mg: Decimal | None


@dataclass(frozen=True)
class ServingValue:
    quantity: Decimal
    unit: ServingUnit


@dataclass(frozen=True)
class AttributionValue:
    text: str
    url: str
    license_url: str = ODBL_URL


@dataclass(frozen=True)
class ProductView:
    kind: Literal["product", "user_product"]
    id: UUID
    source: Literal["off", "usda", "user"]
    source_type: str | None
    name: str
    brand: str | None
    barcode: str | None
    basis: NutrientBasis
    nutrients: NutrientValues
    nutrition_complete: bool
    quality_flags: list[str]
    serving: ServingValue | None
    extra_nutrients: dict[str, Decimal]
    base_product_id: UUID | None
    density_g_per_ml: Decimal | None
    unit_weight_g: Decimal | None
    attribution: AttributionValue | None
    updated_at: datetime
    stale: bool = False


@dataclass(frozen=True)
class SearchView:
    items: list[ProductView] = field(default_factory=list[ProductView])
    partial: bool = False


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --- escaneo ---


async def lookup_barcode(
    session: AsyncSession,
    provider: FoodDataProvider,
    *,
    user_id: UUID,
    code: str,
    settings: Settings,
    now: datetime | None = None,
) -> ProductView | None:
    """Override/custom del usuario -> caché fresca -> caché negativa -> provider.

    `None` = no existe en ningún lado. No se lanza excepción: la caché negativa recién
    escrita debe commitearse, y `get_session` hace rollback ante cualquier excepción
    (el router regresa el 404 como respuesta, no como excepción).
    Si el provider falla: la copia vieja (`stale`) si existe, si no 503.
    """
    gtin = normalize_gtin(code)
    if gtin is None:
        raise InvalidBarcodeError()
    now = now or _utcnow()

    product = await session.scalar(select(Product).where(Product.barcode == gtin))
    user_product = await _user_product_for_barcode(
        session, user_id, gtin, product.id if product else None
    )
    if user_product is not None:
        return _user_product_view(user_product, base=product)

    ttl = timedelta(days=settings.products_cache_ttl_days)
    if (
        product is not None
        and cache_state(product.fetched_at, now=now, ttl=ttl) is CacheState.FRESH
    ):
        return _product_view(product)

    if product is None:
        miss = await session.get(ProductBarcodeMiss, gtin)
        miss_ttl = timedelta(hours=settings.products_miss_ttl_hours)
        if miss is not None and miss_is_fresh(miss.checked_at, now=now, ttl=miss_ttl):
            return None

    # El rollback expira los objetos: lo que se use después se calcula antes.
    stale_view = _product_view(product, stale=True) if product is not None else None
    await _release_before_provider(session)
    try:
        data = await provider.get_by_barcode(gtin)
    except _PROVIDER_FAILURES as exc:
        if stale_view is not None:
            logger.warning("product_served_stale", barcode=gtin, reason=str(exc))
            return stale_view
        retry_after = exc.retry_after if isinstance(exc, ProviderUnavailableError) else None
        raise FoodDataUnavailableError(retry_after=retry_after) from exc

    if data is None:
        if stale_view is not None:
            # Ya no está en la fuente: se sigue sirviendo (hay FKs que la apuntan).
            logger.warning("product_gone_from_source", barcode=gtin)
            await session.execute(
                update(Product).where(Product.id == stale_view.id).values(fetched_at=now)
            )
            return replace(stale_view, stale=False)
        await _record_miss(session, gtin, now)
        return None

    product = await _upsert_from_barcode(session, gtin, data, now)
    return _product_view(product)


async def _release_before_provider(session: AsyncSession) -> None:
    """Cierra la transacción de lectura antes de esperar al provider (hasta 8 s).

    Así no queda una conexión "idle in transaction" ni una transacción larga (ADR
    0004). Precondición, verificada con `has_writes`: solo hubo LECTURAS, así que no
    se pierde nada. Las escrituras van después, en una transacción nueva que commitea
    `get_session`. Ver ADR 0006.
    """
    if has_writes(session):
        # Error de programación (500), no de dominio: el rollback perdería esas escrituras.
        raise RuntimeError("Escrituras antes de llamar al provider; el rollback las perdería")
    await session.rollback()


async def _user_product_for_barcode(
    session: AsyncSession, user_id: UUID, gtin: str, product_id: UUID | None
) -> UserProduct | None:
    match: ColumnElement[bool] = UserProduct.barcode == gtin
    if product_id is not None:
        match = or_(match, UserProduct.base_product_id == product_id)
    return await session.scalar(
        select(UserProduct)
        .where(UserProduct.user_id == user_id, UserProduct.deleted_at.is_(None), match)
        # El que trae el código exacto gana sobre el override por producto base.
        .order_by((UserProduct.barcode == gtin).desc().nulls_last())
        .limit(1)
    )


async def _record_miss(session: AsyncSession, gtin: str, now: datetime) -> None:
    stmt = pg_insert(ProductBarcodeMiss).values(barcode=gtin, checked_at=now)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[ProductBarcodeMiss.barcode], set_={"checked_at": now}
        )
    )


async def _upsert_from_barcode(
    session: AsyncSession, gtin: str, data: ProductData, now: datetime
) -> Product:
    """INSERT ... ON CONFLICT: dos escaneos simultáneos del mismo código no truenan.

    El lookup por barcode es autoritativo: reemplaza lo que hubiera.
    """
    if data.source == "off":
        external_id, barcode = gtin, gtin
    else:  # solo OFF resuelve códigos (ADR 0006, D2); por si acaso no se guarda el código
        external_id, barcode = data.external_id, None
    values = _row_values(
        data, external_id=external_id, barcode=barcode, origin=FetchOrigin.BARCODE, now=now
    )
    stmt = pg_insert(Product).values(id=uuid4(), **values)
    updatable = {k: stmt.excluded[k] for k in values if k not in ("source", "external_id")}
    stmt = stmt.on_conflict_do_update(
        constraint="uq_products_source_external_id",
        set_={**updatable, "updated_at": func.now()},
    ).returning(Product)
    result = await session.scalars(stmt, execution_options={"populate_existing": True})
    return result.one()


# --- búsqueda ---


async def search(
    session: AsyncSession,
    provider: FoodDataProvider,
    *,
    user_id: UUID,
    query: str,
    limit: int,
    now: datetime | None = None,
) -> SearchView:
    """Local primero (productos del usuario y caché); externos solo si no alcanza.

    Lo que llega de OFF/USDA se guarda en la caché (así tiene id estable y la
    siguiente búsqueda ya no sale) y se rankea junto con lo local. Ver ADR 0006.
    """
    now = now or _utcnow()
    query = " ".join(query.split())
    # Misma normalización que `search_text` (la de Postgres, no una copia en Python).
    normalized: str = (
        await session.scalar(select(func.public.immutable_unaccent(func.lower(query))))
    ) or query.lower()
    cap = limit * 3

    user_rows = await _user_candidates(session, user_id, normalized, cap)
    product_rows = await _product_candidates(session, normalized, cap)

    partial = False
    if needs_external_search(len(user_rows) + len(product_rows), limit):
        await _release_before_provider(session)
        try:
            result = await provider.search(query, limit)
        except _PROVIDER_FAILURES as exc:
            logger.warning("food_data_search_failed", reason=str(exc))
            partial = True
        else:
            partial = result.partial
            if result.items:
                await _upsert_from_search(session, result.items, now)
        # El rollback expiró los objetos: se vuelven a leer (ya con lo nuevo).
        user_rows = await _user_candidates(session, user_id, normalized, cap)
        product_rows = await _product_candidates(session, normalized, cap)

    user_products = {up.id: up for up, _ in user_rows}
    products = {p.id: p for p, _ in product_rows}
    overridden = await _overridden_ids(session, user_id, products.keys())
    candidates = [
        *(_user_candidate(up, sim) for up, sim in user_rows),
        *(_product_candidate(p, sim) for p, sim in product_rows),
    ]
    ranked = rank_candidates(drop_overridden(candidates, overridden), normalized)[:limit]

    bases = await _products_by_id(
        session, {up.base_product_id for up in user_products.values() if up.base_product_id}
    )
    items: list[ProductView] = []
    for c in ranked:
        if c.kind == "user_product":
            up = user_products[c.id]
            base = bases.get(up.base_product_id) if up.base_product_id else None
            items.append(_user_product_view(up, base=base))
        else:
            items.append(_product_view(products[c.id]))
    return SearchView(items=items, partial=partial)


def _matches(search_text: InstrumentedAttribute[str], normalized: str) -> ColumnElement[bool]:
    # `<%`: word_similarity >= pg_trgm.word_similarity_threshold (0.6). LIKE cubre
    # subcadenas cortas ("pla" en "platano"). Ambos usan el índice GIN de trigramas.
    pattern = "%" + normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return or_(literal(normalized, Text).op("<%")(search_text), search_text.like(pattern))


async def _user_candidates(
    session: AsyncSession, user_id: UUID, normalized: str, cap: int
) -> list[tuple[UserProduct, float]]:
    similarity = func.word_similarity(normalized, UserProduct.search_text)
    rows = await session.execute(
        select(UserProduct, similarity)
        .where(
            UserProduct.user_id == user_id,
            UserProduct.deleted_at.is_(None),
            _matches(UserProduct.search_text, normalized),
        )
        .order_by(similarity.desc())
        .limit(cap)
    )
    return [(up, float(sim)) for up, sim in rows]


async def _product_candidates(
    session: AsyncSession, normalized: str, cap: int
) -> list[tuple[Product, float]]:
    similarity = func.word_similarity(normalized, Product.search_text)
    rows = await session.execute(
        select(Product, similarity)
        .where(_matches(Product.search_text, normalized))
        .order_by(similarity.desc())
        .limit(cap)
    )
    return [(p, float(sim)) for p, sim in rows]


async def _overridden_ids(
    session: AsyncSession, user_id: UUID, product_ids: Iterable[UUID]
) -> set[UUID]:
    ids = list(product_ids)
    if not ids:
        return set()
    rows = await session.scalars(
        select(UserProduct.base_product_id).where(
            UserProduct.user_id == user_id,
            UserProduct.deleted_at.is_(None),
            UserProduct.base_product_id.in_(ids),
        )
    )
    return {pid for pid in rows if pid is not None}


async def _products_by_id(session: AsyncSession, ids: set[UUID]) -> dict[UUID, Product]:
    if not ids:
        return {}
    rows = await session.scalars(select(Product).where(Product.id.in_(ids)))
    return {p.id: p for p in rows}


def _user_candidate(up: UserProduct, similarity: float) -> SearchCandidate:
    return SearchCandidate(
        kind="user_product",
        id=up.id,
        normalized_name=up.search_text,
        similarity=similarity,
        nutrition_complete=_complete(up),
        flagged=False,
    )


def _product_candidate(p: Product, similarity: float) -> SearchCandidate:
    return SearchCandidate(
        kind="product",
        id=p.id,
        normalized_name=p.search_text,
        similarity=similarity,
        nutrition_complete=_complete(p),
        flagged=bool(p.quality_flags),
        source=p.source.value,
        source_type=p.source_type,
    )


async def _upsert_from_search(
    session: AsyncSession, items: Sequence[ProductData], now: datetime
) -> None:
    """Guarda resultados de búsqueda SIN degradar la caché (`search_upsert_action`).

    1. INSERT ... ON CONFLICT DO NOTHING de todos (nuevos entran directo).
    2. Los que ya existían se bloquean (FOR UPDATE) y se decide uno por uno.
    Los alias de búsqueda (glosario de USDA) se acumulan siempre.
    """
    incoming: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        external_id, barcode = item.external_id, None
        if item.source == "off":
            gtin = normalize_gtin(item.external_id)
            if gtin is not None:
                external_id, barcode = gtin, gtin
        values = _row_values(
            item, external_id=external_id, barcode=barcode, origin=FetchOrigin.SEARCH, now=now
        )
        incoming.setdefault((item.source, external_id), values)
    if not incoming:
        return

    insert_stmt = (
        pg_insert(Product)
        .values([{"id": uuid4(), **values} for values in incoming.values()])
        .on_conflict_do_nothing(constraint="uq_products_source_external_id")
        .returning(Product.source, Product.external_id)
    )
    inserted = {
        (str(source), external_id) for source, external_id in await session.execute(insert_stmt)
    }
    pending = [key for key in incoming if key not in inserted]
    if not pending:
        return

    existing = await session.scalars(
        select(Product)
        .where(tuple_(Product.source, Product.external_id).in_(pending))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    for product in existing:
        values = incoming[(product.source.value, product.external_id)]
        action = search_upsert_action(
            _snapshot_from_model(product), product.fetched_via, _snapshot_from_values(values)
        )
        match action:
            case SearchUpsertAction.REPLACE:
                for key, value in values.items():
                    if key not in ("source", "external_id", "fetched_via", "search_aliases"):
                        setattr(product, key, value)
            case SearchUpsertAction.TOUCH:
                product.fetched_at = now
            case SearchUpsertAction.KEEP:
                pass
        product.search_aliases = _merge_aliases(product.search_aliases, values["search_aliases"])
    await session.flush()


def _merge_aliases(current: str | None, new: str | None) -> str | None:
    terms = dict.fromkeys([*(current or "").split("|"), *(new or "").split("|")])
    merged = "|".join(t for t in terms if t)
    return merged or None


# --- productos custom y overrides ---


async def create_custom(
    session: AsyncSession, user_id: UUID, fields: Mapping[str, Any]
) -> ProductView:
    """`fields` trae solo lo que mandó el cliente (`exclude_unset`)."""
    product_id: UUID = fields.get("id") or uuid4()
    if await session.get(UserProduct, product_id) is not None:
        raise CustomProductIdTakenError()

    values: dict[str, Any] = {}
    base: Product | None = None
    if (base_id := fields.get("base_product_id")) is not None:
        base = await session.get(Product, base_id)
        if base is None:
            raise ProductNotFoundError()
        if await _active_override_exists(session, user_id, base.id):
            raise OverrideExistsError()
        values = _copy_of(base)
    values.update(_custom_values(fields))

    user_product = UserProduct(id=product_id, user_id=user_id, base_product_id=base_id, **values)
    if user_product.barcode is not None:
        await _ensure_barcode_free(session, user_id, user_product.barcode, exclude=None)
    session.add(user_product)
    await _flush_custom(session)
    # Lee lo guardado (escala de Numeric, timestamps) para responder igual que un GET.
    await session.refresh(user_product)
    return _user_product_view(user_product, base=base)


async def get_custom(session: AsyncSession, user_id: UUID, product_id: UUID) -> ProductView:
    user_product = await _get_custom_model(session, user_id, product_id)
    base = (
        await session.get(Product, user_product.base_product_id)
        if user_product.base_product_id
        else None
    )
    return _user_product_view(user_product, base=base)


async def update_custom(
    session: AsyncSession, user_id: UUID, product_id: UUID, fields: Mapping[str, Any]
) -> ProductView:
    user_product = await _get_custom_model(session, user_id, product_id)
    values = _custom_values(fields)
    if values.get("barcode") is not None and values["barcode"] != user_product.barcode:
        await _ensure_barcode_free(session, user_id, values["barcode"], exclude=user_product.id)
    for key, value in values.items():
        setattr(user_product, key, value)
    await _flush_custom(session)
    await session.refresh(user_product)
    return await get_custom(session, user_id, product_id)


async def delete_custom(
    session: AsyncSession, user_id: UUID, product_id: UUID, *, now: datetime | None = None
) -> None:
    """Borrado lógico (tabla sincronizable)."""
    user_product = await _get_custom_model(session, user_id, product_id)
    user_product.deleted_at = now or _utcnow()
    await session.flush()


async def get_product(session: AsyncSession, product_id: UUID) -> ProductView:
    product = await session.get(Product, product_id)
    if product is None:
        raise ProductNotFoundError()
    return _product_view(product)


async def _get_custom_model(session: AsyncSession, user_id: UUID, product_id: UUID) -> UserProduct:
    user_product = await session.scalar(
        select(UserProduct).where(
            UserProduct.id == product_id,
            UserProduct.user_id == user_id,
            UserProduct.deleted_at.is_(None),
        )
    )
    if user_product is None:
        raise CustomProductNotFoundError()
    return user_product


async def _active_override_exists(session: AsyncSession, user_id: UUID, base_id: UUID) -> bool:
    found = await session.scalar(
        select(UserProduct.id).where(
            UserProduct.user_id == user_id,
            UserProduct.base_product_id == base_id,
            UserProduct.deleted_at.is_(None),
        )
    )
    return found is not None


async def _ensure_barcode_free(
    session: AsyncSession, user_id: UUID, barcode: str, *, exclude: UUID | None
) -> None:
    stmt = select(UserProduct.id).where(
        UserProduct.user_id == user_id,
        UserProduct.barcode == barcode,
        UserProduct.deleted_at.is_(None),
    )
    if exclude is not None:
        stmt = stmt.where(UserProduct.id != exclude)
    if await session.scalar(stmt) is not None:
        raise BarcodeTakenError()


async def _flush_custom(session: AsyncSession) -> None:
    """Las carreras que pasan los pre-chequeos las atrapan los índices únicos."""
    try:
        await session.flush()
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)
        if constraint == "uq_user_products_user_id_barcode":
            raise BarcodeTakenError() from exc
        if constraint == "uq_user_products_user_id_base_product_id":
            raise OverrideExistsError() from exc
        if constraint == "pk_user_products":
            raise CustomProductIdTakenError() from exc
        raise


def _custom_values(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Campos del request -> columnas. Valida y normaliza el código de barras."""
    values: dict[str, Any] = {}
    for key in ("name", "brand", "basis", "density_g_per_ml", "unit_weight_g"):
        if key in fields:
            values[key] = fields[key]
    if "barcode" in fields:
        raw = fields["barcode"]
        if raw is None:
            values["barcode"] = None
        else:
            gtin = normalize_gtin(str(raw))
            if gtin is None:
                raise InvalidBarcodeError()
            values["barcode"] = gtin
    if "nutrients" in fields:
        nutrients: Mapping[str, Any] = fields["nutrients"] or {}
        values.update({k: nutrients[k] for k in NUTRIENT_FIELDS if k in nutrients})
    if "serving" in fields:
        serving: Mapping[str, Any] | None = fields["serving"]
        values["serving_quantity"] = serving["quantity"] if serving else None
        values["serving_unit"] = serving["unit"] if serving else None
    return values


def _copy_of(base: Product) -> dict[str, Any]:
    return {
        "name": base.name,
        "brand": base.brand,
        "barcode": base.barcode,
        "basis": base.basis,
        **{key: getattr(base, key) for key in NUTRIENT_FIELDS},
        "extra_nutrients": dict(base.extra_nutrients),
        "serving_quantity": base.serving_quantity,
        "serving_unit": base.serving_unit,
    }


# --- conversión ProductData / modelos ---


def _row_values(
    data: ProductData,
    *,
    external_id: str,
    barcode: str | None,
    origin: FetchOrigin,
    now: datetime,
) -> dict[str, Any]:
    flags = list(data.quality_flags)
    nutrients: dict[str, Decimal | None] = {}
    for key in NUTRIENT_FIELDS:
        value = _fit(getattr(data.nutrients, key))
        if value is None and getattr(data.nutrients, key) is not None:
            flags.append(QualityFlag.VALUE_OUT_OF_RANGE)
        nutrients[key] = value
    return {
        "source": ProductSource(data.source),
        "source_type": data.source_type,
        "external_id": external_id[:64],
        "barcode": barcode,
        "name": data.name[:300],
        "brand": data.brand[:200] if data.brand else None,
        "source_url": data.source_url,
        "basis": NutrientBasis(data.basis),
        **nutrients,
        "extra_nutrients": {
            k: str(v) for k, v in data.extra_nutrients.items() if _fit(v) is not None
        },
        "serving_quantity": _fit(data.serving_quantity),
        "serving_unit": ServingUnit(data.serving_unit) if data.serving_unit else None,
        "quality_flags": list(dict.fromkeys(flags)),
        "search_aliases": "|".join(data.search_aliases) or None,
        "fetched_via": origin,
        "fetched_at": now,
    }


def _fit(value: Decimal | None) -> Decimal | None:
    """Redondea a la escala de la columna; lo que no cabe en Numeric(10,3) -> None."""
    if value is None:
        return None
    rounded = value.quantize(_QUANTUM)
    return rounded if abs(rounded) < _MAX_NUMERIC else None


def _extra_tuple(extra: Mapping[str, Any]) -> tuple[tuple[str, Decimal], ...]:
    return tuple(sorted((k, Decimal(str(v))) for k, v in extra.items()))


def _snapshot_from_model(p: Product) -> ProductSnapshot:
    return ProductSnapshot(
        name=p.name,
        brand=p.brand,
        basis=p.basis,
        **{key: getattr(p, key) for key in NUTRIENT_FIELDS},
        serving_quantity=p.serving_quantity,
        serving_unit=p.serving_unit,
        extra_nutrients=_extra_tuple(p.extra_nutrients),
    )


def _snapshot_from_values(values: Mapping[str, Any]) -> ProductSnapshot:
    return ProductSnapshot(
        name=values["name"],
        brand=values["brand"],
        basis=values["basis"],
        **{key: values[key] for key in NUTRIENT_FIELDS},
        serving_quantity=values["serving_quantity"],
        serving_unit=values["serving_unit"],
        extra_nutrients=_extra_tuple(values["extra_nutrients"]),
    )


def _complete(row: Product | UserProduct) -> bool:
    return None not in (row.kcal, row.protein_g, row.carbs_g, row.fat_g)


def _nutrients(row: Product | UserProduct) -> NutrientValues:
    return NutrientValues(**{key: getattr(row, key) for key in NUTRIENT_FIELDS})


def _serving(row: Product | UserProduct) -> ServingValue | None:
    if row.serving_quantity is None or row.serving_unit is None:
        return None
    return ServingValue(quantity=row.serving_quantity, unit=row.serving_unit)


def _attribution(product: Product | None) -> AttributionValue | None:
    if product is None or product.source is not ProductSource.OFF or not product.source_url:
        return None
    return AttributionValue(text=OFF_ATTRIBUTION_TEXT, url=product.source_url)


def _product_view(product: Product, *, stale: bool = False) -> ProductView:
    return ProductView(
        kind="product",
        id=product.id,
        source="off" if product.source is ProductSource.OFF else "usda",
        source_type=product.source_type,
        name=product.name,
        brand=product.brand,
        barcode=product.barcode,
        basis=product.basis,
        nutrients=_nutrients(product),
        nutrition_complete=_complete(product),
        quality_flags=list(product.quality_flags),
        serving=_serving(product),
        extra_nutrients={k: Decimal(str(v)) for k, v in product.extra_nutrients.items()},
        base_product_id=None,
        density_g_per_ml=None,
        unit_weight_g=None,
        attribution=_attribution(product),
        updated_at=product.updated_at,
        stale=stale,
    )


def _user_product_view(user_product: UserProduct, *, base: Product | None) -> ProductView:
    # Un override de OFF sigue derivando de datos de OFF: lleva su atribución.
    if base is not None and base.id != user_product.base_product_id:
        base = None
    return ProductView(
        kind="user_product",
        id=user_product.id,
        source="user",
        source_type=None,
        name=user_product.name,
        brand=user_product.brand,
        barcode=user_product.barcode,
        basis=user_product.basis,
        nutrients=_nutrients(user_product),
        nutrition_complete=_complete(user_product),
        quality_flags=[],
        serving=_serving(user_product),
        extra_nutrients={k: Decimal(str(v)) for k, v in user_product.extra_nutrients.items()},
        base_product_id=user_product.base_product_id,
        density_g_per_ml=user_product.density_g_per_ml,
        unit_weight_g=user_product.unit_weight_g,
        attribution=_attribution(base),
        updated_at=user_product.updated_at,
    )
