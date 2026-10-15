from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.modules.products.domain import NutrientBasis, ServingUnit

# Cabe en Numeric(10, 3).
NutrientValue = Annotated[Decimal, Field(ge=0, lt=Decimal(10_000_000), decimal_places=3)]
PositiveAmount = Annotated[Decimal, Field(gt=0, lt=Decimal(10_000_000), decimal_places=3)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Brand = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Density = Annotated[Decimal, Field(gt=0, lt=Decimal(100), decimal_places=4)]


class Nutrients(BaseModel):
    """Por 100 g o 100 ml según `basis`. `carbs_g` = carbohidratos disponibles (sin fibra)."""

    kcal: Decimal | None = None
    protein_g: Decimal | None = None
    carbs_g: Decimal | None = None
    fat_g: Decimal | None = None
    fiber_g: Decimal | None = None
    sugar_g: Decimal | None = None
    sodium_mg: Decimal | None = None


class Serving(BaseModel):
    quantity: Decimal
    unit: ServingUnit


class Attribution(BaseModel):
    """Obligatoria para datos de Open Food Facts (ODbL). Ver docs/CLIENT_NOTES.md."""

    text: str
    url: str
    license_url: str


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kind: Literal["product", "user_product"]
    id: UUID
    source: Literal["off", "usda", "user"]
    source_type: str | None
    name: str
    brand: str | None
    barcode: str | None = Field(description="GTIN-14 (14 dígitos)")
    basis: NutrientBasis
    nutrients: Nutrients
    nutrition_complete: bool = Field(description="kcal, proteína, carbohidratos y grasa")
    quality_flags: list[str]
    serving: Serving | None
    extra_nutrients: dict[str, Decimal]
    base_product_id: UUID | None = Field(description="Si es un override de un producto global")
    density_g_per_ml: Decimal | None
    unit_weight_g: Decimal | None
    stale: bool = Field(
        description="Copia vieja de la caché: el proveedor no respondió al refrescarla"
    )
    attribution: Attribution | None
    updated_at: datetime


class SearchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    items: list[ProductOut]
    partial: bool = Field(description="Alguna fuente externa no respondió o no tenía cuota")


class NutrientsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kcal: NutrientValue | None = None
    protein_g: NutrientValue | None = None
    carbs_g: NutrientValue | None = None
    fat_g: NutrientValue | None = None
    fiber_g: NutrientValue | None = None
    sugar_g: NutrientValue | None = None
    sodium_mg: NutrientValue | None = None


class ServingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quantity: PositiveAmount
    unit: ServingUnit


class _CustomFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    brand: Brand | None = None
    barcode: str | None = Field(default=None, description="UPC-A, EAN-8, EAN-13 o GTIN-14")
    basis: NutrientBasis | None = None
    nutrients: NutrientsIn | None = None
    serving: ServingIn | None = None
    density_g_per_ml: Density | None = None
    unit_weight_g: PositiveAmount | None = None


class CustomProductCreate(_CustomFields):
    # Ids generados por el cliente (sync offline).
    id: UUID | None = None
    # Override: se copia el producto global y se aplican los campos enviados.
    base_product_id: UUID | None = None

    @model_validator(mode="after")
    def _require_basics_without_base(self) -> Self:
        if self.base_product_id is None and (self.name is None or self.basis is None):
            raise ValueError("`name` y `basis` son obligatorios si no hay `base_product_id`")
        return self


class CustomProductUpdate(_CustomFields):
    @model_validator(mode="after")
    def _reject_null_required(self) -> Self:
        for field in ("name", "basis"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"`{field}` no puede ser null")
        return self
