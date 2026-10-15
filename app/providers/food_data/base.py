from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol

FoodDataSource = Literal["off", "usda"]
# Los nutrientes van por 100 g o por 100 ml. Nunca se asume una densidad. Ver ADR 0006.
NutrientBasis = Literal["per_100g", "per_100ml"]
ServingUnit = Literal["g", "ml"]


@dataclass(frozen=True)
class Nutrients:
    """Por 100 de la base (`ProductData.basis`). `None` = no reportado (no se inventa).

    `carbs_g` son carbohidratos DISPONIBLES (sin fibra), como OFF y la NOM-051.
    """

    kcal: Decimal | None = None
    protein_g: Decimal | None = None
    carbs_g: Decimal | None = None
    fat_g: Decimal | None = None
    fiber_g: Decimal | None = None
    sugar_g: Decimal | None = None
    sodium_mg: Decimal | None = None

    @property
    def complete(self) -> bool:
        """kcal y las 3 macros. Sin ellas no se puede calcular nutrición confiable."""
        return None not in (self.kcal, self.protein_g, self.carbs_g, self.fat_g)


@dataclass(frozen=True)
class ProductData:
    source: FoodDataSource
    external_id: str
    name: str
    basis: NutrientBasis
    nutrients: Nutrients
    # Código tal como lo reporta la fuente (sin normalizar ni validar).
    barcode: str | None = None
    brand: str | None = None
    # Subtipo de la fuente; en USDA es el `dataType` (Foundation, SR Legacy...).
    source_type: str | None = None
    serving_quantity: Decimal | None = None
    serving_unit: ServingUnit | None = None
    extra_nutrients: Mapping[str, Decimal] = field(default_factory=dict[str, Decimal])
    # Datos sospechosos detectados al normalizar; se marcan, no se corrigen.
    quality_flags: tuple[str, ...] = ()
    # Términos de búsqueda (en español) que llevaron a este resultado. Los usa el
    # glosario de USDA para que la caché local lo encuentre después en español.
    search_aliases: tuple[str, ...] = ()
    # Página pública del producto en la fuente (atribución de OFF; ver ADR 0006).
    source_url: str | None = None

    @property
    def nutrition_complete(self) -> bool:
        return self.nutrients.complete


@dataclass(frozen=True)
class SearchResult:
    items: list[ProductData]
    # Fuentes que fallaron o no tenían cuota: el resultado es parcial.
    unavailable_sources: frozenset[FoodDataSource] = frozenset()

    @property
    def partial(self) -> bool:
        return bool(self.unavailable_sources)


class FoodDataProvider(Protocol):
    """Las implementaciones normalizan a 100 g / 100 ml y a nuestras unidades.

    Errores: `ProviderUnavailableError` / `ProviderBadDataError` (fallas del
    proveedor) o `ProviderError` (config o bug nuestro). Nunca excepciones de httpx.
    """

    async def get_by_barcode(self, gtin: str) -> ProductData | None:
        """`gtin`: GTIN-14 ya validado. `None` = el proveedor no lo tiene."""
        ...

    async def search(self, query: str, limit: int = 20) -> SearchResult: ...
