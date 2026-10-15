from decimal import Decimal

import pytest

from app.providers.errors import ProviderBadDataError, ProviderError, ProviderUnavailableError
from app.providers.food_data import Nutrients, ProductData
from app.providers.food_data.chained import ChainedFoodDataProvider
from tests.fakes.food_data import FakeFoodDataProvider

GTIN = "07501000000001"


def _product(source: str, name: str = "Avena") -> ProductData:
    return ProductData(
        source="off" if source == "off" else "usda",
        external_id=f"{source}-1",
        name=name,
        basis="per_100g",
        barcode="7501000000001",
        nutrients=Nutrients(kcal=Decimal(389)),
    )


async def test_barcode_first_provider_wins_and_stops() -> None:
    first = FakeFoodDataProvider(products=[_product("off")])
    second = FakeFoodDataProvider(products=[_product("usda")])
    chain = ChainedFoodDataProvider([("off", first), ("usda", second)])

    product = await chain.get_by_barcode(GTIN)

    assert product is not None
    assert product.source == "off"
    assert second.barcode_lookups == []


async def test_barcode_falls_through_on_not_found() -> None:
    first = FakeFoodDataProvider()
    second = FakeFoodDataProvider(products=[_product("usda")])
    chain = ChainedFoodDataProvider([("off", first), ("usda", second)])

    product = await chain.get_by_barcode(GTIN)

    assert product is not None
    assert product.source == "usda"
    assert first.barcode_lookups == second.barcode_lookups == [GTIN]


@pytest.mark.parametrize(
    "error", [ProviderUnavailableError("off", "timeout"), ProviderBadDataError("off", "basura")]
)
async def test_barcode_falls_through_on_provider_failure(error: ProviderError) -> None:
    chain = ChainedFoodDataProvider(
        [
            ("off", FakeFoodDataProvider(barcode_error=error)),
            ("usda", FakeFoodDataProvider(products=[_product("usda")])),
        ]
    )
    product = await chain.get_by_barcode(GTIN)
    assert product is not None
    assert product.source == "usda"


@pytest.mark.parametrize(
    "error", [RuntimeError("bug nuestro"), ProviderError("off: HTTP 401 en product")]
)
async def test_barcode_does_not_fall_through_on_our_errors(error: Exception) -> None:
    second = FakeFoodDataProvider(products=[_product("usda")])
    chain = ChainedFoodDataProvider(
        [("off", FakeFoodDataProvider(barcode_error=error)), ("usda", second)]
    )
    with pytest.raises(type(error)):
        await chain.get_by_barcode(GTIN)
    assert second.barcode_lookups == []


async def test_barcode_not_found_everywhere_is_none() -> None:
    chain = ChainedFoodDataProvider(
        [("off", FakeFoodDataProvider()), ("usda", FakeFoodDataProvider())]
    )
    assert await chain.get_by_barcode(GTIN) is None


async def test_barcode_not_found_with_a_failure_is_unavailable_not_none() -> None:
    # Sin esto, una caída de OFF terminaría en caché negativa ("no existe").
    chain = ChainedFoodDataProvider(
        [
            (
                "off",
                FakeFoodDataProvider(
                    barcode_error=ProviderUnavailableError("off", "x", retry_after=30)
                ),
            ),
            ("usda", FakeFoodDataProvider()),
        ]
    )
    with pytest.raises(ProviderUnavailableError) as exc_info:
        await chain.get_by_barcode(GTIN)
    assert exc_info.value.retry_after == 30


async def test_search_merges_in_order() -> None:
    chain = ChainedFoodDataProvider(
        [
            ("off", FakeFoodDataProvider(products=[_product("off", "Avena OFF")])),
            ("usda", FakeFoodDataProvider(products=[_product("usda", "Avena USDA")])),
        ]
    )
    result = await chain.search("avena")
    assert [p.name for p in result.items] == ["Avena OFF", "Avena USDA"]
    assert not result.partial


async def test_search_tolerates_failures_as_partial() -> None:
    chain = ChainedFoodDataProvider(
        [
            ("off", FakeFoodDataProvider(search_error=ProviderUnavailableError("off", "429"))),
            ("usda", FakeFoodDataProvider(products=[_product("usda", "Avena USDA")])),
        ]
    )
    result = await chain.search("avena")
    assert [p.name for p in result.items] == ["Avena USDA"]
    assert result.unavailable_sources == {"off"}
    assert result.partial


async def test_search_raises_our_bugs() -> None:
    chain = ChainedFoodDataProvider(
        [
            ("off", FakeFoodDataProvider(search_error=KeyError("bug"))),
            ("usda", FakeFoodDataProvider()),
        ]
    )
    with pytest.raises(KeyError):
        await chain.search("avena")
