from decimal import Decimal

import httpx
import pytest

from app.core.config import Settings
from app.providers.food_data.budget import OutboundBudget
from app.providers.food_data.usda import GENERIC_TYPES, UsdaProvider

pytestmark = pytest.mark.external


@pytest.fixture
def usda(http_client: httpx.AsyncClient, real_settings: Settings) -> UsdaProvider:
    if real_settings.usda_api_key is None:
        pytest.skip("USDA_API_KEY no definida")
    return UsdaProvider(
        http_client,
        base_url=real_settings.usda_base_url,
        api_key=real_settings.usda_api_key.get_secret_value(),
        budget=OutboundBudget("usda", {"search": "10/minute"}),
        timeout=15.0,
    )


async def test_generic_food_in_spanish_via_glossary(usda: UsdaProvider) -> None:
    # También valida que la key en el header `X-Api-Key` funciona (si no: 403 -> error).
    result = await usda.search("plátano", limit=5)

    assert result.items
    generic = [p for p in result.items if p.source_type in GENERIC_TYPES]
    assert generic, "se esperaban genéricos (Foundation / SR Legacy / FNDDS)"
    for product in result.items:
        assert product.source == "usda"
        assert product.external_id.isdigit()
        assert product.name.strip()
        assert product.search_aliases == ("platano",)
        for value in vars(product.nutrients).values():
            assert value is None or isinstance(value, Decimal)
    first = generic[0]
    assert first.basis == "per_100g"
    # Si esto falla, cambiaron los ids de nutrientes (ver usda.py y ADR 0006).
    assert first.nutrients.kcal is not None
    assert first.nutrients.protein_g is not None
