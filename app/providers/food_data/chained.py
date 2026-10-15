import asyncio
from collections.abc import Sequence

import structlog

from app.providers.errors import ProviderBadDataError, ProviderUnavailableError
from app.providers.food_data.base import (
    FoodDataProvider,
    FoodDataSource,
    ProductData,
    SearchResult,
)

logger = structlog.get_logger(__name__)

# Fallas DEL PROVEEDOR: se toleran y se pasa al siguiente. Cualquier otra excepción
# (ProviderError por config, o un bug nuestro) sube tal cual.
_TOLERATED = (ProviderUnavailableError, ProviderBadDataError)


class ChainedFoodDataProvider:
    """Prueba los providers en orden. Ver ADR 0006.

    - `get_by_barcode`: el primero que lo tenga gana. Pasa al siguiente si regresa
      `None` o si falla con `_TOLERATED`. Si nadie lo tiene y alguno falló, lanza
      `ProviderUnavailableError`: "no existe" solo se afirma si todos respondieron.
    - `search`: consulta a todos en paralelo y concatena en orden. Los que fallan
      quedan en `unavailable_sources` (resultado parcial).
    """

    def __init__(self, providers: Sequence[tuple[FoodDataSource, FoodDataProvider]]) -> None:
        self._providers = list(providers)

    async def get_by_barcode(self, gtin: str) -> ProductData | None:
        failure: ProviderUnavailableError | ProviderBadDataError | None = None
        for source, provider in self._providers:
            try:
                product = await provider.get_by_barcode(gtin)
            except _TOLERATED as exc:
                logger.warning("food_data_provider_failed", source=source, reason=str(exc))
                failure = exc
                continue
            if product is not None:
                return product
        if failure is not None:
            retry_after = (
                failure.retry_after if isinstance(failure, ProviderUnavailableError) else None
            )
            raise ProviderUnavailableError(
                "chain", "ningún provider respondió", retry_after=retry_after
            ) from failure
        return None

    async def search(self, query: str, limit: int = 20) -> SearchResult:
        outcomes = await asyncio.gather(
            *(provider.search(query, limit) for _, provider in self._providers),
            return_exceptions=True,
        )
        items: list[ProductData] = []
        unavailable: set[FoodDataSource] = set()
        for (source, _), outcome in zip(self._providers, outcomes, strict=True):
            if isinstance(outcome, _TOLERATED):
                logger.warning("food_data_provider_failed", source=source, reason=str(outcome))
                unavailable.add(source)
            elif isinstance(outcome, BaseException):
                raise outcome
            else:
                items.extend(outcome.items)
                unavailable.update(outcome.unavailable_sources)
        return SearchResult(items=items, unavailable_sources=frozenset(unavailable))
