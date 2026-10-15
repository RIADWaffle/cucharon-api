"""OpenFoodFactsProvider con JSON reales (tests/fixtures/off) y httpx.MockTransport."""

from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from typing import Any, Protocol

import httpx
import pytest

from app.providers.errors import ProviderBadDataError, ProviderError, ProviderUnavailableError
from app.providers.food_data import QualityFlag
from app.providers.food_data.budget import OutboundBudget
from app.providers.food_data.off import OpenFoodFactsProvider, off_code
from tests.support import load_fixture

Handler = Callable[[httpx.Request], httpx.Response]


class MakeProvider(Protocol):
    def __call__(
        self, handler: Handler, limits: dict[str, str] | None = None
    ) -> OpenFoodFactsProvider: ...


NUTELLA = "03017620422003"


@pytest.fixture
async def make_provider() -> AsyncIterator[MakeProvider]:
    clients: list[httpx.AsyncClient] = []

    def make(handler: Handler, limits: dict[str, str] | None = None) -> OpenFoodFactsProvider:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        budget = OutboundBudget("off", limits or {"product": "100/minute", "search": "100/minute"})
        return OpenFoodFactsProvider(
            client, base_url="https://off.test", budget=budget, timeout=2.0
        )

    yield make
    for client in clients:
        await client.aclose()


def _json(fixture: str, status: int = 200) -> Handler:
    body: Any = load_fixture(f"off/{fixture}.json")
    return lambda _: httpx.Response(status, json=body)


async def test_solid_product_per_100g(make_provider: MakeProvider) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _json("product_solid")(request)

    product = await make_provider(handler).get_by_barcode(NUTELLA)

    assert product is not None
    assert requests[0].url.path == "/api/v3.4/product/3017620422003"
    assert requests[0].url.params["lc"] == "es"
    assert product.source == "off"
    assert product.external_id == product.barcode == "3017620422003"
    assert product.name == "Nutella"
    assert product.brand == "Nutella"
    assert product.basis == "per_100g"
    assert product.nutrients.kcal == Decimal(539)
    assert product.nutrients.protein_g == Decimal("6.3")
    assert product.nutrients.carbs_g == Decimal("57.5")
    assert product.nutrients.fat_g == Decimal("30.9")
    assert product.nutrients.sugar_g == Decimal("56.3")
    assert product.nutrients.sodium_mg == Decimal("42.8")  # OFF lo da en gramos
    assert product.extra_nutrients["saturated_fat_g"] == Decimal("10.6")
    assert product.nutrition_complete
    assert product.quality_flags == ()
    assert product.serving_quantity is None  # sin cantidad no se inventa
    assert product.source_url == "https://world.openfoodfacts.org/product/3017620422003"


async def test_beverage_with_ml_quantity_is_per_100ml(make_provider: MakeProvider) -> None:
    # `nutrition_data_per` dice "100g", pero OFF documenta que `_100g` es 100 ml en líquidos.
    product = await make_provider(_json("product_beverage_quantity_ml")).get_by_barcode(
        "05449000000996"
    )
    assert product is not None
    assert product.basis == "per_100ml"
    assert product.nutrients.kcal == Decimal("42.4")
    assert product.nutrients.fiber_g is None
    assert (product.serving_quantity, product.serving_unit) == (Decimal(330), "ml")


async def test_beverage_declared_per_100ml(make_provider: MakeProvider) -> None:
    product = await make_provider(_json("product_beverage_per_100ml")).get_by_barcode(
        "03502110009357"
    )
    assert product is not None
    assert product.basis == "per_100ml"
    assert product.name == "Pepsi Max"  # sin product_name_es: cae a product_name
    assert product.brand == "Pepsi"


async def test_kj_only_is_converted_to_kcal(make_provider: MakeProvider) -> None:
    product = await make_provider(_json("product_kj_only")).get_by_barcode(NUTELLA)
    assert product is not None
    assert product.nutrients.kcal == Decimal("538.2")  # 2252 kJ / 4.184


async def test_missing_energy_stays_none_and_incomplete(make_provider: MakeProvider) -> None:
    body = load_fixture("off/product_solid.json")
    nutriments = body["product"]["nutriments"]
    for key in list(nutriments):
        if key.startswith("energy"):
            del nutriments[key]
    product = await make_provider(lambda _: httpx.Response(200, json=body)).get_by_barcode(NUTELLA)
    assert product is not None
    assert product.nutrients.kcal is None
    assert not product.nutrition_complete


async def test_beverage_category_without_units_is_flagged(make_provider: MakeProvider) -> None:
    body = load_fixture("off/product_beverage_quantity_ml.json")
    for key in ("product_quantity_unit", "serving_quantity_unit"):
        body["product"].pop(key)
    product = await make_provider(lambda _: httpx.Response(200, json=body)).get_by_barcode(
        "05449000000996"
    )
    assert product is not None
    assert product.basis == "per_100g"
    assert QualityFlag.BASIS_UNCERTAIN in product.quality_flags


async def test_not_found(make_provider: MakeProvider) -> None:
    provider = make_provider(_json("product_not_found", status=404))
    assert await provider.get_by_barcode("07509999888772") is None


async def test_search_in_spanish(make_provider: MakeProvider) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _json("search_platano")(request)

    result = await make_provider(handler).search("plátano", limit=3)

    params = requests[0].url.params
    assert requests[0].url.path == "/cgi/search.pl"
    assert params["search_terms"] == "plátano"
    assert params["json"] == "1"
    assert params["lc"] == "es"
    assert params["page_size"] == "3"
    assert [p.name for p in result.items][:1] == ["Kéfir Fresa Plátano"]
    assert len(result.items) == 3
    assert not result.partial


async def test_search_skips_broken_items(make_provider: MakeProvider) -> None:
    body = load_fixture("off/search_platano.json")
    body["products"][0].pop("code")
    body["products"].append("no soy un objeto")
    result = await make_provider(lambda _: httpx.Response(200, json=body)).search("platano")
    assert len(result.items) == len(body["products"]) - 2


@pytest.mark.parametrize(
    ("gtin", "code"),
    [
        ("03017620422003", "3017620422003"),  # EAN-13
        ("00034000470693", "0034000470693"),  # UPC-A -> 13 dígitos, como OFF
        ("00000012345670", "12345670"),  # EAN-8
        ("10012345678902", "10012345678902"),  # GTIN-14 de caja
    ],
)
def test_off_code(gtin: str, code: str) -> None:
    assert off_code(gtin) == code


# --- errores: nada de httpx sale del provider ---


async def test_timeout_is_unavailable(make_provider: MakeProvider) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lento", request=request)

    with pytest.raises(ProviderUnavailableError, match="ReadTimeout"):
        await make_provider(handler).get_by_barcode(NUTELLA)


async def test_connection_error_is_unavailable(make_provider: MakeProvider) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin red", request=request)

    with pytest.raises(ProviderUnavailableError):
        await make_provider(handler).search("avena")


async def test_server_error_is_unavailable(make_provider: MakeProvider) -> None:
    with pytest.raises(ProviderUnavailableError, match="HTTP 502"):
        await make_provider(lambda _: httpx.Response(502)).get_by_barcode(NUTELLA)


async def test_throttling_pauses_that_kind_of_request(make_provider: MakeProvider) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/cgi/search.pl":
            return httpx.Response(503, headers={"Retry-After": "120"}, text="overloaded")
        return _json("product_solid")(request)

    provider = make_provider(handler)
    with pytest.raises(ProviderUnavailableError) as exc_info:
        await provider.search("avena")
    assert exc_info.value.retry_after == 120
    # En pausa: ni siquiera sale la request.
    with pytest.raises(ProviderUnavailableError, match="pausa"):
        await provider.search("avena")
    assert calls == ["/cgi/search.pl"]
    # Las lecturas de producto no se pausan por la búsqueda.
    assert await provider.get_by_barcode(NUTELLA) is not None


async def test_outbound_budget_exhausted_skips_request(make_provider: MakeProvider) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _json("product_solid")(request)

    provider = make_provider(handler, {"product": "1/minute", "search": "1/minute"})
    await provider.get_by_barcode(NUTELLA)
    with pytest.raises(ProviderUnavailableError, match="cuota") as exc_info:
        await provider.get_by_barcode(NUTELLA)
    assert exc_info.value.retry_after is not None
    assert len(calls) == 1


async def test_api_client_error_is_our_bug_not_unavailable(make_provider: MakeProvider) -> None:
    rejected = httpx.Response(400, json={"error": "invalid parameter"})
    with pytest.raises(ProviderError) as exc_info:
        await make_provider(lambda _: rejected).get_by_barcode(NUTELLA)
    assert not isinstance(exc_info.value, ProviderUnavailableError | ProviderBadDataError)


async def test_proxy_client_error_without_json_is_unavailable(make_provider: MakeProvider) -> None:
    # Así llegan los 400 intermitentes de nginx en USDA (ADR 0006).
    nginx = httpx.Response(400, text="<html><center>nginx</center></html>")
    with pytest.raises(ProviderUnavailableError, match="sin JSON"):
        await make_provider(lambda _: nginx).get_by_barcode(NUTELLA)


async def test_non_json_is_bad_data(make_provider: MakeProvider) -> None:
    with pytest.raises(ProviderBadDataError):
        await make_provider(lambda _: httpx.Response(200, text="<html>")).get_by_barcode(NUTELLA)


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"status": "failure", "result": {"id": "invalid_code"}},
        {"status": "success"},
    ],
)
async def test_unexpected_product_body_is_bad_data(
    make_provider: MakeProvider, body: object
) -> None:
    with pytest.raises(ProviderBadDataError):
        await make_provider(lambda _: httpx.Response(200, json=body)).get_by_barcode(NUTELLA)


async def test_unexpected_search_body_is_bad_data(make_provider: MakeProvider) -> None:
    with pytest.raises(ProviderBadDataError):
        await make_provider(lambda _: httpx.Response(200, json={"count": 0})).search("x")
