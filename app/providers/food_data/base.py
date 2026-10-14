from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol

FoodDataSource = Literal["off", "usda"]


@dataclass(frozen=True)
class Nutrients:
    """Por 100 g. `None` = el proveedor no lo reporta (no se inventa)."""

    kcal: Decimal | None = None
    protein_g: Decimal | None = None
    carbs_g: Decimal | None = None
    fat_g: Decimal | None = None
    fiber_g: Decimal | None = None
    sugar_g: Decimal | None = None
    sodium_mg: Decimal | None = None


@dataclass(frozen=True)
class ProductData:
    source: FoodDataSource
    external_id: str
    name: str
    nutrients_per_100g: Nutrients
    barcode: str | None = None
    brand: str | None = None
    serving_size_g: Decimal | None = None
    extra_nutrients: Mapping[str, Decimal] = field(default_factory=dict[str, Decimal])

    @property
    def nutrition_complete(self) -> bool:
        return self.nutrients_per_100g.kcal is not None


class FoodDataProvider(Protocol):
    """Las implementaciones normalizan los valores a 100 g."""

    async def get_by_barcode(self, code: str) -> ProductData | None: ...

    async def search(self, query: str, limit: int = 20) -> list[ProductData]: ...
