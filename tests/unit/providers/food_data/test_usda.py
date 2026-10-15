"""UsdaProvider con JSON reales (tests/fixtures/usda) y httpx.MockTransport."""

import json
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from typing import Any, Protocol

import httpx
import pytest

from app.providers.errors import ProviderBadDataError, ProviderUnavailableError
from app.providers.food_data import QualityFlag
from app.providers.food_data.budget import OutboundBudget
from app.providers.food_data.usda import UsdaProvider, parse_food, translate_query
from tests.support import load_fixture

Handler = Callable[[httpx.Request], httpx.Response]


class MakeProvider(Protocol):
    def __call__(self, handler: Handler) -> UsdaProvider: ...


@pytest.fixture
async def make_provider() -> AsyncIterator[MakeProvider]:
    clients: list[httpx.AsyncClient] = []

    def make(handler: Handler) -> UsdaProvider:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return UsdaProvider(
            client,
            base_url="https://usda.test/fdc/v1",
            api_key="secreta",
            budget=OutboundBudget("usda", {"search": "100/minute"}),
            timeout=2.0,
        )

    yield make
    for client in clients:
        await client.aclose()


def _food(fixture: str, fdc_id: int) -> dict[str, Any]:
    foods: list[dict[str, Any]] = load_fixture(f"usda/{fixture}.json")["foods"]
    return next(f for f in foods if f["fdcId"] == fdc_id)


def _body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


async def test_search_posts_generic_types_with_key_in_header(make_provider: MakeProvider) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=load_fixture("usda/search_generic_banana.json"))

    result = await make_provider(handler).search("bananas raw", limit=4)

    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/fdc/v1/foods/search"
    assert request.headers["X-Api-Key"] == "secreta"
    assert "api_key" not in request.url.params  # la key no va en la URL (logs)
    assert _body(request) == {
        "query": "bananas raw",
        "dataType": ["Foundation", "SR Legacy", "Survey (FNDDS)"],
        "pageSize": 4,
    }
    # Foundation > SR Legacy > FNDDS, respetando el orden de USDA dentro de cada tipo.
    assert [(p.source_type, p.name) for p in result.items] == [
        ("Foundation", "Bananas, overripe, raw"),
        ("SR Legacy", "Bananas, raw"),
        ("SR Legacy", "Pepper, banana, raw"),
        ("Survey (FNDDS)", "Banana, raw"),
    ]


def test_sr_legacy_food_carbs_are_available_carbs() -> None:
    banana = parse_food(_food("search_generic_banana", 173944))
    assert banana.source == "usda"
    assert banana.external_id == "173944"
    assert banana.basis == "per_100g"
    assert banana.nutrients.kcal == Decimal("89.0")
    # "Carbohydrate, by difference" incluye fibra: 22.8 - 2.6.
    assert banana.nutrients.carbs_g == Decimal("20.2")
    assert banana.nutrients.fiber_g == Decimal("2.6")
    assert banana.nutrients.sodium_mg == Decimal("1.0")
    assert banana.nutrition_complete
    assert banana.quality_flags == ()
    assert banana.barcode is None


def test_foundation_food_without_1008_uses_atwater_specific() -> None:
    apple = parse_food(_food("search_foundation_apple", 1750340))
    assert apple.nutrients.kcal == Decimal("58.2")  # 2048 antes que 2047
    assert apple.nutrients.carbs_g == Decimal("15.7") - Decimal("2.08")


@pytest.mark.parametrize("fdc_id", [2642521, 2385707])  # servingSizeUnit "MLT" y "ml"
def test_branded_drink_is_per_100ml(fdc_id: int) -> None:
    milk = parse_food(_food("search_branded_milk", fdc_id))
    assert milk.source_type == "Branded"
    assert milk.basis == "per_100ml"
    assert (milk.serving_quantity, milk.serving_unit) == (Decimal("240.0"), "ml")
    assert milk.brand is not None
    assert milk.nutrients.kcal == Decimal("62.0")


def test_missing_fiber_keeps_by_difference_and_flags_it() -> None:
    food = _food("search_generic_banana", 173944)
    food["foodNutrients"] = [n for n in food["foodNutrients"] if n["nutrientId"] != 1079]
    banana = parse_food(food)
    assert banana.nutrients.carbs_g == Decimal("22.8")
    assert QualityFlag.CARBS_MAY_INCLUDE_FIBER in banana.quality_flags


def test_kj_only_energy_is_converted() -> None:
    food = _food("search_generic_banana", 173944)
    food["foodNutrients"] = [n for n in food["foodNutrients"] if n["nutrientId"] != 1008]
    assert parse_food(food).nutrients.kcal == Decimal("88.7")  # 371 kJ / 4.184


def test_branded_without_serving_unit_is_flagged() -> None:
    food = _food("search_branded_milk", 2642521)
    food.pop("servingSizeUnit")
    milk = parse_food(food)
    assert milk.basis == "per_100g"
    assert QualityFlag.BASIS_UNCERTAIN in milk.quality_flags
    assert milk.serving_quantity is None


def test_sodium_reported_in_grams_is_converted() -> None:
    food = _food("search_generic_banana", 173944)
    for nutrient in food["foodNutrients"]:
        if nutrient["nutrientId"] == 1093:
            nutrient.update(value=0.5, unitName="G")
    assert parse_food(food).nutrients.sodium_mg == Decimal("500.0")


def test_food_without_id_or_name_is_bad_data() -> None:
    with pytest.raises(ProviderBadDataError):
        parse_food({"description": "Sin id"})
    with pytest.raises(ProviderBadDataError):
        parse_food({"fdcId": 1, "description": "  "})


async def test_spanish_query_uses_glossary_and_aliases(make_provider: MakeProvider) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=load_fixture("usda/search_generic_banana.json"))

    result = await make_provider(handler).search("Plátanos", limit=4)

    assert _body(requests[0])["query"] == "bananas raw"
    assert all(item.search_aliases == ("platano",) for item in result.items)


async def test_branded_only_when_no_generic_results(make_provider: MakeProvider) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if _body(request)["dataType"] == ["Branded"]:
            return httpx.Response(200, json=load_fixture("usda/search_branded_milk.json"))
        return httpx.Response(200, json=load_fixture("usda/search_empty.json"))

    result = await make_provider(handler).search("whole milk", limit=3)

    assert [_body(r)["dataType"] for r in requests] == [
        ["Foundation", "SR Legacy", "Survey (FNDDS)"],
        ["Branded"],
    ]
    assert len(result.items) == 3


async def test_no_branded_call_when_generic_results_exist(make_provider: MakeProvider) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=load_fixture("usda/search_generic_banana.json"))

    await make_provider(handler).search("bananas raw")
    assert len(calls) == 1


async def test_barcodes_are_not_resolved_by_usda(make_provider: MakeProvider) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("USDA no debe recibir lookups por código")

    assert await make_provider(handler).get_by_barcode("00034000470693") is None


async def test_nginx_400_is_unavailable(make_provider: MakeProvider) -> None:
    nginx = httpx.Response(400, text="<html><center>nginx</center></html>")
    with pytest.raises(ProviderUnavailableError):
        await make_provider(lambda _: nginx).search("rice")


async def test_unexpected_body_is_bad_data(make_provider: MakeProvider) -> None:
    with pytest.raises(ProviderBadDataError):
        await make_provider(lambda _: httpx.Response(200, json={"totalHits": 0})).search("rice")


async def test_broken_items_are_skipped(make_provider: MakeProvider) -> None:
    body = load_fixture("usda/search_generic_banana.json")
    body["foods"][0].pop("fdcId")
    body["foods"].append(["no", "objeto"])
    result = await make_provider(lambda _: httpx.Response(200, json=body)).search("bananas")
    assert len(result.items) == 3


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("plátano", ("bananas raw", ("platano",))),
        ("  PLATANOS ", ("bananas raw", ("platano",))),
        ("limones", ("limes raw", ("limon",))),
        ("aguacates", ("avocados raw", ("aguacate",))),
        ("pechuga de pollo", ("chicken broiler breast meat only raw", ("pechuga de pollo",))),
        ("whole milk", None),
        ("plátano con crema", None),
    ],
)
def test_translate_query(query: str, expected: tuple[str, tuple[str, ...]] | None) -> None:
    assert translate_query(query) == expected
