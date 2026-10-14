from dataclasses import dataclass, field

from app.providers.food_data import FoodDataProvider, ProductData


@dataclass
class FakeFoodDataProvider:
    """Catálogo en memoria. `search` = substring case-insensitive en el nombre."""

    products: list[ProductData] = field(default_factory=list[ProductData])
    barcode_lookups: list[str] = field(default_factory=list[str])
    search_queries: list[str] = field(default_factory=list[str])

    async def get_by_barcode(self, code: str) -> ProductData | None:
        self.barcode_lookups.append(code)
        return next((p for p in self.products if p.barcode == code), None)

    async def search(self, query: str, limit: int = 20) -> list[ProductData]:
        self.search_queries.append(query)
        needle = query.casefold()
        return [p for p in self.products if needle in p.name.casefold()][:limit]


_: type[FoodDataProvider] = FakeFoodDataProvider
