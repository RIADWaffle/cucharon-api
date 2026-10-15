from dataclasses import dataclass, field

from app.providers.food_data import FoodDataProvider, FoodDataSource, ProductData, SearchResult


@dataclass
class FakeFoodDataProvider:
    """Catálogo en memoria.

    - `get_by_barcode`: compara el GTIN-14 con `barcode` rellenado a 14 dígitos.
    - `search`: substring case-insensitive en el nombre.
    - `*_error`: si está, se lanza en lugar de responder (fallas del proveedor o bugs).
    """

    products: list[ProductData] = field(default_factory=list[ProductData])
    barcode_error: BaseException | None = None
    search_error: BaseException | None = None
    unavailable_sources: frozenset[FoodDataSource] = frozenset()
    barcode_lookups: list[str] = field(default_factory=list[str])
    search_queries: list[str] = field(default_factory=list[str])

    async def get_by_barcode(self, gtin: str) -> ProductData | None:
        self.barcode_lookups.append(gtin)
        if self.barcode_error is not None:
            raise self.barcode_error
        return next(
            (p for p in self.products if p.barcode is not None and p.barcode.zfill(14) == gtin),
            None,
        )

    async def search(self, query: str, limit: int = 20) -> SearchResult:
        self.search_queries.append(query)
        if self.search_error is not None:
            raise self.search_error
        needle = query.casefold()
        items = [p for p in self.products if needle in p.name.casefold()][:limit]
        return SearchResult(items=items, unavailable_sources=self.unavailable_sources)


_: type[FoodDataProvider] = FakeFoodDataProvider
