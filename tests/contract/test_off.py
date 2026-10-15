from decimal import Decimal

import httpx
import pytest

from app.core.config import Settings
from app.providers.errors import ProviderUnavailableError
from app.providers.food_data import ProductData
from app.providers.food_data.budget import OutboundBudget
from app.providers.food_data.off import OpenFoodFactsProvider

pytestmark = pytest.mark.external

NUTELLA = "03017620422003"  # el ejemplo de la documentación de OFF
COCA_COLA = "05449000000996"
UNKNOWN = "07509999888772"


@pytest.fixture
def off(http_client: httpx.AsyncClient, real_settings: Settings) -> OpenFoodFactsProvider:
    budget = OutboundBudget("off", {"product": "10/minute", "search": "2/minute"})
    return OpenFoodFactsProvider(
        http_client, base_url=real_settings.off_base_url, budget=budget, timeout=15.0
    )


def _assert_shape(product: ProductData) -> None:
    assert product.source == "off"
    assert product.external_id.isdigit()
    assert product.name.strip()
    assert product.basis in ("per_100g", "per_100ml")
    for value in vars(product.nutrients).values():
        assert value is None or isinstance(value, Decimal)
    assert product.source_url is not None
    assert product.source_url.startswith("https://world.openfoodfacts.org/product/")


async def test_known_barcode(off: OpenFoodFactsProvider) -> None:
    product = await off.get_by_barcode(NUTELLA)
    assert product is not None
    _assert_shape(product)
    assert product.nutrients.kcal is not None  # si esto falla, cambió `nutriments`


async def test_known_beverage_is_per_100ml(off: OpenFoodFactsProvider) -> None:
    product = await off.get_by_barcode(COCA_COLA)
    assert product is not None
    _assert_shape(product)
    assert product.basis == "per_100ml"


async def test_unknown_barcode(off: OpenFoodFactsProvider) -> None:
    assert await off.get_by_barcode(UNKNOWN) is None


async def test_search_in_spanish(off: OpenFoodFactsProvider) -> None:
    try:
        result = await off.search("plátano", limit=5)
    except ProviderUnavailableError as exc:
        # search.pl responde 503 cuando OFF está saturado; no es un cambio de contrato.
        pytest.xfail(f"OFF search no disponible: {exc}")
    assert result.items
    for product in result.items:
        _assert_shape(product)
