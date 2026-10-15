from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Computed,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin, text_enum
from app.modules.products.domain import FetchOrigin, NutrientBasis, ProductSource, ServingUnit

# Cantidades por 100 g o 100 ml (según `basis`). Ver ADR 0006.
NUTRIENT = Numeric(10, 3)


class NutrientColumns:
    """Columnas compartidas por `products` y `user_products`."""

    basis: Mapped[NutrientBasis] = mapped_column(text_enum(NutrientBasis, "basis"))
    kcal: Mapped[Decimal | None] = mapped_column(NUTRIENT)
    protein_g: Mapped[Decimal | None] = mapped_column(NUTRIENT)
    # Carbohidratos DISPONIBLES (sin fibra), como OFF y la NOM-051.
    carbs_g: Mapped[Decimal | None] = mapped_column(NUTRIENT)
    fat_g: Mapped[Decimal | None] = mapped_column(NUTRIENT)
    fiber_g: Mapped[Decimal | None] = mapped_column(NUTRIENT)
    sugar_g: Mapped[Decimal | None] = mapped_column(NUTRIENT)
    sodium_mg: Mapped[Decimal | None] = mapped_column(NUTRIENT)
    # Decimales como texto: JSON no tiene Decimal y float pierde exactitud.
    extra_nutrients: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    serving_quantity: Mapped[Decimal | None] = mapped_column(NUTRIENT)
    serving_unit: Mapped[ServingUnit | None] = mapped_column(text_enum(ServingUnit, "serving_unit"))


class Product(NutrientColumns, TimestampMixin, Base):
    """Caché global de OFF y USDA. No es sincronizable ni se borra (hay FKs a ella)."""

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_products_source_external_id"),
        CheckConstraint("barcode ~ '^[0-9]{14}$'", name="barcode_gtin14"),
        # Solo OFF resuelve códigos, y su external_id ES el GTIN-14. Ver ADR 0006 (D2).
        CheckConstraint(
            "barcode IS NULL OR (source = 'off' AND external_id = barcode)",
            name="barcode_only_off",
        ),
        Index(
            "ix_products_search_text_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source: Mapped[ProductSource] = mapped_column(text_enum(ProductSource, "source"))
    source_type: Mapped[str | None] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(64))
    # Índice NO único: la unicidad la garantizan el CHECK `barcode_only_off` y
    # unique(source, external_id). Un segundo índice único rompería el ON CONFLICT
    # (solo arbitra uno). Ver ADR 0006 (D5).
    barcode: Mapped[str | None] = mapped_column(String(14), index=True)
    name: Mapped[str] = mapped_column(String(300))
    brand: Mapped[str | None] = mapped_column(String(200))
    source_url: Mapped[str | None] = mapped_column(String(500))
    quality_flags: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), default=list, server_default=text("'{}'")
    )
    # Términos en español que llevaron a un resultado de USDA (glosario).
    search_aliases: Mapped[str | None] = mapped_column(Text)
    search_text: Mapped[str] = mapped_column(
        Text,
        Computed(
            "public.immutable_unaccent(lower("
            "name || ' ' || coalesce(brand, '') || ' ' || coalesce(search_aliases, '')"
            "))",
            persisted=True,
        ),
    )
    fetched_via: Mapped[FetchOrigin] = mapped_column(text_enum(FetchOrigin, "fetched_via"))
    fetched_at: Mapped[datetime]


class UserProduct(NutrientColumns, TimestampMixin, Base):
    """Producto custom del usuario, u override de uno global (`base_product_id`).

    Sincronizable: acepta ids generados por el cliente y se borra lógicamente.
    """

    __tablename__ = "user_products"
    __table_args__ = (
        CheckConstraint("barcode ~ '^[0-9]{14}$'", name="barcode_gtin14"),
        CheckConstraint("density_g_per_ml > 0", name="density_positive"),
        CheckConstraint("unit_weight_g > 0", name="unit_weight_positive"),
        Index(
            "uq_user_products_user_id_barcode",
            "user_id",
            "barcode",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND barcode IS NOT NULL"),
        ),
        Index(
            "uq_user_products_user_id_base_product_id",
            "user_id",
            "base_product_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND base_product_id IS NOT NULL"),
        ),
        Index(
            "ix_user_products_search_text_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    base_product_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String(300))
    brand: Mapped[str | None] = mapped_column(String(200))
    barcode: Mapped[str | None] = mapped_column(String(14))
    # La única fuente de densidad: nunca se asume 1 g/ml. Ver ADR 0006.
    density_g_per_ml: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    unit_weight_g: Mapped[Decimal | None] = mapped_column(NUTRIENT)
    search_text: Mapped[str] = mapped_column(
        Text,
        Computed(
            "public.immutable_unaccent(lower(name || ' ' || coalesce(brand, '')))",
            persisted=True,
        ),
    )
    deleted_at: Mapped[datetime | None]


class ProductBarcodeMiss(Base):
    """Caché negativa: códigos que ningún provider tiene. Ver ADR 0006 (D4)."""

    __tablename__ = "product_barcode_misses"
    __table_args__ = (CheckConstraint("barcode ~ '^[0-9]{14}$'", name="barcode_gtin14"),)

    barcode: Mapped[str] = mapped_column(String(14), primary_key=True)
    checked_at: Mapped[datetime]
