"""Open Food Facts. Doc: https://openfoodfacts.github.io/openfoodfacts-server/api/ (ADR 0006)."""

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, cast

import httpx
import structlog

from app.providers.errors import ProviderBadDataError
from app.providers.food_data._http import request_json
from app.providers.food_data.base import (
    NutrientBasis,
    Nutrients,
    ProductData,
    SearchResult,
    ServingUnit,
)
from app.providers.food_data.budget import OutboundBudget
from app.providers.food_data.normalize import (
    QualityFlag,
    assess_quality,
    clean_text,
    kj_to_kcal,
    salt_to_sodium_mg,
    to_decimal,
)

logger = structlog.get_logger(__name__)

PROVIDER = "off"
# Versión fijada: desde v3.5 `nutriments` viene vacío y los nutrientes pasan a una
# estructura nueva que OFF marca "under active development". Ver ADR 0006.
PRODUCT_PATH = "/api/v3.4/product/{code}"
# Búsqueda de texto: solo existe en el endpoint legado (v2 es solo filtros).
SEARCH_PATH = "/cgi/search.pl"
FIELDS = ",".join(
    [
        "code",
        "product_name",
        "product_name_es",
        "generic_name_es",
        "brands",
        "product_quantity_unit",
        "serving_quantity",
        "serving_quantity_unit",
        "nutrition_data_per",
        "categories_tags",
        "nutriments",
    ]
)
BEVERAGE_CATEGORY = "en:beverages"
# Página pública (atribución ODbL). Siempre producción, aunque la API apunte a staging.
PUBLIC_PRODUCT_URL = "https://world.openfoodfacts.org/product/{code}"


class OpenFoodFactsProvider:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        budget: OutboundBudget,
        timeout: float,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._budget = budget
        self._timeout = timeout

    async def get_by_barcode(self, gtin: str) -> ProductData | None:
        response = await request_json(
            self._client,
            provider=PROVIDER,
            kind="product",
            budget=self._budget,
            url=self._base_url + PRODUCT_PATH.format(code=off_code(gtin)),
            params={"fields": FIELDS, "lc": "es"},
            max_seconds=self._timeout,
        )
        body = response.body
        if response.status_code == 404:
            return None
        if not isinstance(body, dict):
            raise ProviderBadDataError(PROVIDER, "producto: el cuerpo no es un objeto")
        body = cast(dict[str, Any], body)
        if body.get("status") == "failure":
            result = body.get("result")
            if isinstance(result, dict) and result.get("id") == "product_not_found":
                return None
            raise ProviderBadDataError(PROVIDER, f"producto: status failure ({result!r})")
        product = body.get("product")
        if not isinstance(product, dict):
            raise ProviderBadDataError(PROVIDER, "producto: falta `product`")
        return parse_product(cast(dict[str, Any], product))

    async def search(self, query: str, limit: int = 20) -> SearchResult:
        response = await request_json(
            self._client,
            provider=PROVIDER,
            kind="search",
            budget=self._budget,
            url=self._base_url + SEARCH_PATH,
            params={
                "search_terms": query,
                "search_simple": 1,
                "action": "process",
                "json": 1,
                "page_size": limit,
                "fields": FIELDS,
                "lc": "es",
            },
            max_seconds=self._timeout,
        )
        body = response.body
        if response.status_code == 404 or not isinstance(body, dict):
            raise ProviderBadDataError(PROVIDER, "búsqueda: respuesta inesperada")
        products = cast(dict[str, Any], body).get("products")
        if not isinstance(products, list):
            raise ProviderBadDataError(PROVIDER, "búsqueda: falta `products`")
        items: list[ProductData] = []
        for raw in cast(list[Any], products):
            if not isinstance(raw, dict):
                continue
            try:
                items.append(parse_product(cast(dict[str, Any], raw)))
            except ProviderBadDataError as exc:
                # Un resultado roto no tumba la búsqueda completa.
                logger.warning("off_search_item_skipped", reason=exc.reason)
        return SearchResult(items=items[:limit])


def off_code(gtin: str) -> str:
    """GTIN-14 -> código como lo guarda OFF (EAN-8 de 8 dígitos, EAN-13/UPC de 13)."""
    if gtin.startswith("000000"):
        return gtin[6:]
    if gtin.startswith("0"):
        return gtin[1:]
    return gtin


def parse_product(product: Mapping[str, Any]) -> ProductData:
    code = clean_text(str(product.get("code") or ""))
    if code is None:
        raise ProviderBadDataError(PROVIDER, "producto sin `code`")
    raw_nutriments = product.get("nutriments")
    nutriments: Mapping[str, Any] = (
        cast(dict[str, Any], raw_nutriments) if isinstance(raw_nutriments, dict) else {}
    )

    basis, basis_uncertain = _basis(product)
    alcohol = to_decimal(nutriments.get("alcohol_100g"))
    nutrients = Nutrients(
        kcal=_kcal(nutriments),
        protein_g=to_decimal(nutriments.get("proteins_100g")),
        carbs_g=to_decimal(nutriments.get("carbohydrates_100g")),
        fat_g=to_decimal(nutriments.get("fat_100g")),
        fiber_g=to_decimal(nutriments.get("fiber_100g")),
        sugar_g=to_decimal(nutriments.get("sugars_100g")),
        sodium_mg=_sodium_mg(nutriments),
    )
    extra: dict[str, Decimal] = {}
    if (saturated := to_decimal(nutriments.get("saturated-fat_100g"))) is not None:
        extra["saturated_fat_g"] = saturated
    if alcohol is not None:
        extra["alcohol_g"] = alcohol

    flags = list(assess_quality(nutrients, basis, alcohol_g=alcohol))
    if basis_uncertain:
        flags.append(QualityFlag.BASIS_UNCERTAIN)

    serving_quantity, serving_unit = _serving(product)
    name = (
        clean_text(product.get("product_name_es"))
        or clean_text(product.get("product_name"))
        or clean_text(product.get("generic_name_es"))
        or code
    )
    return ProductData(
        source="off",
        external_id=code,
        name=name,
        basis=basis,
        nutrients=nutrients,
        barcode=code,
        brand=_first_brand(product.get("brands")),
        serving_quantity=serving_quantity,
        serving_unit=serving_unit,
        extra_nutrients=extra,
        quality_flags=tuple(flags),
        source_url=PUBLIC_PRODUCT_URL.format(code=code),
    )


def _kcal(nutriments: Mapping[str, Any]) -> Decimal | None:
    kcal = to_decimal(nutriments.get("energy-kcal_100g"))
    if kcal is not None:
        return kcal
    # `energy_100g` es kJ ("same as energy-kj if we have it").
    kj = to_decimal(nutriments.get("energy-kj_100g"))
    if kj is None:
        kj = to_decimal(nutriments.get("energy_100g"))
    return kj_to_kcal(kj) if kj is not None else None


def _sodium_mg(nutriments: Mapping[str, Any]) -> Decimal | None:
    # OFF normaliza `_100g` a gramos.
    sodium_g = to_decimal(nutriments.get("sodium_100g"))
    if sodium_g is not None:
        return sodium_g * 1000
    salt_g = to_decimal(nutriments.get("salt_100g"))
    return salt_to_sodium_mg(salt_g) if salt_g is not None else None


def _basis(product: Mapping[str, Any]) -> tuple[NutrientBasis, bool]:
    """`_100g` significa 100 ml en líquidos; OFF no tiene un flag directo."""
    units = {product.get("product_quantity_unit"), product.get("serving_quantity_unit")}
    if product.get("nutrition_data_per") == "100ml" or "ml" in units:
        return "per_100ml", False
    raw_categories = product.get("categories_tags")
    categories = cast(list[Any], raw_categories) if isinstance(raw_categories, list) else []
    looks_like_drink = BEVERAGE_CATEGORY in categories
    has_unit = bool(units & {"g", "ml"})
    return "per_100g", looks_like_drink and not has_unit


def _serving(product: Mapping[str, Any]) -> tuple[Decimal | None, ServingUnit | None]:
    quantity = to_decimal(product.get("serving_quantity"))
    unit = product.get("serving_quantity_unit")
    if quantity is None or quantity <= 0 or unit not in ("g", "ml"):
        return None, None  # sin unidad no se adivina
    return quantity, unit


def _first_brand(value: object) -> str | None:
    text = clean_text(value)
    if text is None:
        return None
    return clean_text(text.split(",")[0])
