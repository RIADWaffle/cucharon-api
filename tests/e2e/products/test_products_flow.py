from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI

from app.core.rate_limit import KeyedRateLimiter
from app.providers.errors import ProviderUnavailableError
from app.providers.food_data import Nutrients, ProductData
from tests.fakes.food_data import FakeFoodDataProvider

UNKNOWN = "7509999888772"


async def _auth(client: httpx.AsyncClient, email: str = "ana@example.com") -> dict[str, str]:
    password = "correct horse battery"
    response = await client.post(
        "/auth/register",
        json={"email": email, "password": password, "timezone": "America/Mexico_City"},
    )
    assert response.status_code == 201, response.text
    response = await client.post("/auth/login", json={"email": email, "password": password})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _coke() -> ProductData:
    return ProductData(
        source="off",
        external_id="5449000000996",
        name="Coca-Cola Original",
        basis="per_100ml",
        barcode="5449000000996",
        nutrients=Nutrients(
            kcal=Decimal("42.4"), protein_g=Decimal(0), carbs_g=Decimal("10.6"), fat_g=Decimal(0)
        ),
        serving_quantity=Decimal(330),
        serving_unit="ml",
        source_url="https://world.openfoodfacts.org/product/5449000000996",
    )


async def test_scan_unknown_then_create_custom_then_scan_returns_custom(
    client: httpx.AsyncClient, fake_food_data: FakeFoodDataProvider
) -> None:
    headers = await _auth(client)

    response = await client.get(f"/products/barcode/{UNKNOWN}", headers=headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "product_not_found"
    # La caché negativa quedó commiteada: el segundo escaneo no sale al provider.
    response = await client.get(f"/products/barcode/{UNKNOWN}", headers=headers)
    assert response.status_code == 404

    response = await client.post(
        "/products/custom",
        headers=headers,
        json={
            "name": "Salsa de la abuela",
            "basis": "per_100g",
            "barcode": UNKNOWN,
            "nutrients": {"kcal": "35", "protein_g": "1.2", "carbs_g": "6", "fat_g": "0.4"},
        },
    )
    assert response.status_code == 201, response.text
    custom = response.json()

    response = await client.get(f"/products/barcode/{UNKNOWN}", headers=headers)
    assert response.status_code == 200
    scanned = response.json()
    assert scanned["id"] == custom["id"]
    assert scanned["kind"] == "user_product"
    assert scanned["source"] == "user"
    assert scanned["barcode"] == "0" + UNKNOWN
    assert scanned["nutrition_complete"] is True
    # El segundo escaneo no volvió a preguntarle al provider.
    assert fake_food_data.barcode_lookups == ["0" + UNKNOWN]


async def test_scan_beverage_from_off(
    client: httpx.AsyncClient, fake_food_data: FakeFoodDataProvider
) -> None:
    headers = await _auth(client)
    fake_food_data.products.append(_coke())

    response = await client.get("/products/barcode/5449000000996", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "product"
    assert body["basis"] == "per_100ml"
    assert body["nutrients"]["kcal"] == "42.400"
    assert body["serving"] == {"quantity": "330.000", "unit": "ml"}
    assert body["stale"] is False
    assert body["attribution"]["url"].endswith("5449000000996")
    assert "Open Food Facts" in body["attribution"]["text"]

    response = await client.get(f"/products/{body['id']}", headers=headers)
    assert response.status_code == 200
    assert response.json()["name"] == "Coca-Cola Original"


async def test_invalid_barcode_is_422_without_provider(
    client: httpx.AsyncClient, fake_food_data: FakeFoodDataProvider
) -> None:
    headers = await _auth(client)
    response = await client.get("/products/barcode/5449000000997", headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_barcode"
    assert fake_food_data.barcode_lookups == []


async def test_provider_down_is_503_with_retry_after(
    client: httpx.AsyncClient, fake_food_data: FakeFoodDataProvider
) -> None:
    headers = await _auth(client)
    fake_food_data.barcode_error = ProviderUnavailableError("off", "429", retry_after=12.5)
    response = await client.get("/products/barcode/5449000000996", headers=headers)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "food_data_unavailable"
    assert response.headers["retry-after"] == "13"


async def test_search_endpoint(
    client: httpx.AsyncClient, fake_food_data: FakeFoodDataProvider
) -> None:
    headers = await _auth(client)
    fake_food_data.products.append(_coke())

    response = await client.get("/products/search", params={"q": "coca"}, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["partial"] is False
    assert [item["name"] for item in body["items"]] == ["Coca-Cola Original"]

    response = await client.get("/products/search", params={"q": "c"}, headers=headers)
    assert response.status_code == 422


async def test_custom_crud_endpoints(client: httpx.AsyncClient) -> None:
    headers = await _auth(client)
    other = await _auth(client, "beto@example.com")

    response = await client.post(
        "/products/custom",
        headers=headers,
        json={"name": "Licuado", "basis": "per_100ml", "density_g_per_ml": "1.04"},
    )
    assert response.status_code == 201
    product_id = response.json()["id"]

    response = await client.patch(
        f"/products/custom/{product_id}", headers=headers, json={"nutrients": {"kcal": "50"}}
    )
    assert response.status_code == 200
    assert response.json()["nutrients"]["kcal"] == "50.000"
    assert response.json()["nutrition_complete"] is False

    response = await client.patch(
        f"/products/custom/{product_id}", headers=headers, json={"name": None}
    )
    assert response.status_code == 422

    response = await client.get(f"/products/custom/{product_id}", headers=other)
    assert response.status_code == 404

    response = await client.delete(f"/products/custom/{product_id}", headers=headers)
    assert response.status_code == 204
    response = await client.get(f"/products/custom/{product_id}", headers=headers)
    assert response.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"basis": "per_100g"},  # sin nombre ni producto base
        {"name": "x", "basis": "per_100g", "nutrients": {"kcal": "-1"}},
        {"name": "x", "basis": "per_100g", "unknown": 1},
        {"name": "x", "basis": "per_100l"},
    ],
)
async def test_custom_validation(client: httpx.AsyncClient, body: dict[str, object]) -> None:
    headers = await _auth(client)
    response = await client.post("/products/custom", headers=headers, json=body)
    assert response.status_code == 422


async def test_products_require_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/products/search", params={"q": "avena"})
    assert response.status_code == 401


async def test_barcode_is_rate_limited_per_user(
    app: FastAPI, client: httpx.AsyncClient, fake_food_data: FakeFoodDataProvider
) -> None:
    app.state.products_barcode_limiter = KeyedRateLimiter("2/minute")
    headers = await _auth(client)
    other = await _auth(client, "beto@example.com")
    fake_food_data.products.append(_coke())

    for _ in range(2):
        response = await client.get("/products/barcode/5449000000996", headers=headers)
        assert response.status_code == 200
    response = await client.get("/products/barcode/5449000000996", headers=headers)
    assert response.status_code == 429
    # Otro usuario tiene su propia cuota.
    response = await client.get("/products/barcode/5449000000996", headers=other)
    assert response.status_code == 200
