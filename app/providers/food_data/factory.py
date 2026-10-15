import httpx
import structlog
from fastapi import Request

from app.core.config import Settings
from app.providers.errors import ProviderUnavailableError
from app.providers.food_data.base import FoodDataProvider, FoodDataSource, SearchResult
from app.providers.food_data.budget import OutboundBudget
from app.providers.food_data.chained import ChainedFoodDataProvider
from app.providers.food_data.off import OpenFoodFactsProvider
from app.providers.food_data.usda import UsdaProvider

logger = structlog.get_logger(__name__)


class NoFoodDataProvider:
    """Sin providers configurados: no hay datos externos. No afirma "no existe"."""

    async def get_by_barcode(self, gtin: str) -> None:
        raise ProviderUnavailableError("none", "FOOD_DATA_PROVIDERS vacío")

    async def search(self, query: str, limit: int = 20) -> SearchResult:
        return SearchResult(items=[])


def build_food_data_provider(settings: Settings, client: httpx.AsyncClient) -> FoodDataProvider:
    """Arma la cadena según `FOOD_DATA_PROVIDERS` (se llama una vez, en el lifespan)."""
    chain: list[tuple[FoodDataSource, FoodDataProvider]] = []
    timeout = settings.food_data_timeout_seconds
    for source in settings.food_data_providers:
        match source:
            case "off":
                budget = OutboundBudget(
                    "off",
                    {
                        "product": settings.off_rate_limit_product,
                        "search": settings.off_rate_limit_search,
                    },
                )
                chain.append(
                    (
                        "off",
                        OpenFoodFactsProvider(
                            client, base_url=settings.off_base_url, budget=budget, timeout=timeout
                        ),
                    )
                )
            case "usda":
                if settings.usda_api_key is None:
                    logger.warning("usda_disabled", reason="USDA_API_KEY vacía")
                    continue
                budget = OutboundBudget("usda", {"search": settings.usda_rate_limit})
                chain.append(
                    (
                        "usda",
                        UsdaProvider(
                            client,
                            base_url=settings.usda_base_url,
                            api_key=settings.usda_api_key.get_secret_value(),
                            budget=budget,
                            timeout=timeout,
                        ),
                    )
                )
    if not chain:
        return NoFoodDataProvider()
    return ChainedFoodDataProvider(chain)


def get_food_data_provider(request: Request) -> FoodDataProvider:
    """Dependency de FastAPI: la cadena creada en el lifespan."""
    return request.app.state.food_data_provider
