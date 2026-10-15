"""USDA FoodData Central. Doc: https://fdc.nal.usda.gov/api-guide/ (ADR 0006).

Solo alimentos genéricos por texto: USDA no se usa para códigos de barras.
La búsqueda de USDA es en inglés; las consultas en español se traducen con un
glosario curado (`usda_glossary_es.json`), sin LLM.
"""

import json
import unicodedata
from collections.abc import Mapping, Sequence
from decimal import Decimal
from functools import cache
from importlib import resources
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
    to_decimal,
)

logger = structlog.get_logger(__name__)

PROVIDER = "usda"
SEARCH_PATH = "/foods/search"
BRANDED = "Branded"
# Orden de preferencia. Los genéricos van primero; Branded solo si no hubo genéricos.
GENERIC_TYPES = ("Foundation", "SR Legacy", "Survey (FNDDS)")
TYPE_PRIORITY = {name: i for i, name in enumerate((*GENERIC_TYPES, BRANDED))}

# `nutrientId` de FDC (confirmados en respuestas reales; ver ADR 0006).
ENERGY_KCAL = 1008
ENERGY_ATWATER_SPECIFIC = 2048  # Foundation ya no trae 1008
ENERGY_ATWATER_GENERAL = 2047
ENERGY_KJ = 1062
PROTEIN = 1003
FAT = 1004
CARBS_BY_DIFFERENCE = 1005  # INCLUYE fibra
FIBER = 1079
SUGARS_TOTAL = 2000
SUGARS_TOTAL_ALT = 1063
SODIUM = 1093

_UNIT_BY_SERVING_UNIT: dict[str, ServingUnit] = {"g": "g", "grm": "g", "ml": "ml", "mlt": "ml"}


class UsdaProvider:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        api_key: str,
        budget: OutboundBudget,
        timeout: float,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._budget = budget
        self._timeout = timeout

    async def get_by_barcode(self, gtin: str) -> None:
        # USDA solo aporta genéricos; los códigos los resuelve OFF. Ver ADR 0006 (D2).
        return None

    async def search(self, query: str, limit: int = 20) -> SearchResult:
        translation = translate_query(query)
        english, aliases = translation if translation else (query, ())
        items = await self._search(english, GENERIC_TYPES, limit, aliases)
        if not items:
            items = await self._search(english, (BRANDED,), limit, aliases)
        items.sort(key=lambda item: TYPE_PRIORITY.get(item.source_type or "", len(TYPE_PRIORITY)))
        return SearchResult(items=items[:limit])

    async def _search(
        self, query: str, data_types: Sequence[str], limit: int, aliases: tuple[str, ...]
    ) -> list[ProductData]:
        # POST con JSON (documentado): el GET equivalente recibe 400 intermitentes de
        # nginx (observado: 4 de 6 intentos), el POST no. Ver ADR 0006.
        response = await request_json(
            self._client,
            method="POST",
            provider=PROVIDER,
            kind="search",
            budget=self._budget,
            url=self._base_url + SEARCH_PATH,
            json_body={"query": query, "dataType": list(data_types), "pageSize": limit},
            # Header y no query param: la key no queda en logs de URLs.
            headers={"X-Api-Key": self._api_key},
            max_seconds=self._timeout,
        )
        body = response.body
        if response.status_code == 404 or not isinstance(body, dict):
            raise ProviderBadDataError(PROVIDER, "búsqueda: respuesta inesperada")
        foods = cast(dict[str, Any], body).get("foods")
        if not isinstance(foods, list):
            raise ProviderBadDataError(PROVIDER, "búsqueda: falta `foods`")
        items: list[ProductData] = []
        for raw in cast(list[Any], foods):
            if not isinstance(raw, dict):
                continue
            try:
                items.append(parse_food(cast(dict[str, Any], raw), aliases=aliases))
            except ProviderBadDataError as exc:
                logger.warning("usda_search_item_skipped", reason=exc.reason)
        return items


def parse_food(food: Mapping[str, Any], *, aliases: tuple[str, ...] = ()) -> ProductData:
    fdc_id = food.get("fdcId")
    name = clean_text(food.get("description"))
    if not isinstance(fdc_id, int) or name is None:
        raise ProviderBadDataError(PROVIDER, "alimento sin `fdcId` o `description`")
    data_type = clean_text(food.get("dataType"))
    values = _nutrient_values(food.get("foodNutrients"))

    flags: list[QualityFlag] = []
    by_difference = values.get(CARBS_BY_DIFFERENCE)
    fiber = values.get(FIBER)
    carbs: Decimal | None = None
    if by_difference is not None:
        if fiber is not None:
            carbs = by_difference - fiber  # disponibles, como OFF y la NOM-051
        else:
            carbs = by_difference
            flags.append(QualityFlag.CARBS_MAY_INCLUDE_FIBER)

    basis, basis_uncertain = _basis(data_type, food.get("servingSizeUnit"))
    sugars = values.get(SUGARS_TOTAL)
    nutrients = Nutrients(
        kcal=_kcal(values),
        protein_g=values.get(PROTEIN),
        carbs_g=carbs,
        fat_g=values.get(FAT),
        fiber_g=fiber,
        sugar_g=sugars if sugars is not None else values.get(SUGARS_TOTAL_ALT),
        sodium_mg=values.get(SODIUM),
    )
    flags = [*assess_quality(nutrients, basis), *flags]
    if basis_uncertain:
        flags.append(QualityFlag.BASIS_UNCERTAIN)

    serving_quantity, serving_unit = _serving(food)
    return ProductData(
        source="usda",
        external_id=str(fdc_id),
        name=name,
        basis=basis,
        nutrients=nutrients,
        barcode=clean_text(food.get("gtinUpc")),
        brand=clean_text(food.get("brandName")) or clean_text(food.get("brandOwner")),
        source_type=data_type,
        serving_quantity=serving_quantity,
        serving_unit=serving_unit,
        quality_flags=tuple(flags),
        search_aliases=aliases,
    )


def _nutrient_values(raw: object) -> dict[int, Decimal]:
    """`nutrientId` -> valor, en g / mg / kcal según el nutriente (kJ se deja aparte)."""
    values: dict[int, Decimal] = {}
    if not isinstance(raw, list):
        return values
    for entry in cast(list[Any], raw):
        if not isinstance(entry, dict):
            continue
        entry = cast(dict[str, Any], entry)
        nutrient_id = entry.get("nutrientId")
        value = to_decimal(entry.get("value"))
        if not isinstance(nutrient_id, int) or value is None:
            continue
        unit = str(entry.get("unitName") or "").lower()
        if nutrient_id == SODIUM and unit == "g":
            value *= 1000
        values[nutrient_id] = value
    return values


def _kcal(values: Mapping[int, Decimal]) -> Decimal | None:
    for nutrient_id in (ENERGY_KCAL, ENERGY_ATWATER_SPECIFIC, ENERGY_ATWATER_GENERAL):
        if (kcal := values.get(nutrient_id)) is not None:
            return kcal
    kj = values.get(ENERGY_KJ)
    return kj_to_kcal(kj) if kj is not None else None


def _basis(data_type: str | None, serving_size_unit: object) -> tuple[NutrientBasis, bool]:
    # Foundation, SR Legacy y FNDDS: siempre por 100 g de porción comestible.
    if data_type != BRANDED:
        return "per_100g", False
    # Branded: "100-unit basis, either gram (g) or milliliter (ml)". No hay un campo
    # documentado; se deduce de la unidad de la porción (observado: "MLT").
    unit = _UNIT_BY_SERVING_UNIT.get(str(serving_size_unit or "").lower())
    if unit == "ml":
        return "per_100ml", False
    return "per_100g", unit is None


def _serving(food: Mapping[str, Any]) -> tuple[Decimal | None, ServingUnit | None]:
    quantity = to_decimal(food.get("servingSize"))
    unit = _UNIT_BY_SERVING_UNIT.get(str(food.get("servingSizeUnit") or "").lower())
    if quantity is None or quantity <= 0 or unit is None:
        return None, None
    return quantity, unit


def normalize_term(text: str) -> str:
    """Minúsculas, sin acentos y con espacios simples (igual que `immutable_unaccent`)."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.split())


@cache
def _glossary() -> dict[str, str]:
    raw = resources.files("app.providers.food_data").joinpath("usda_glossary_es.json")
    data = cast(dict[str, str], json.loads(raw.read_text(encoding="utf-8")))
    return {normalize_term(es): en for es, en in data.items()}


def translate_query(query: str) -> tuple[str, tuple[str, ...]] | None:
    """Consulta en español -> (consulta en inglés para USDA, alias en español).

    Coincidencia exacta con el glosario, o quitando un plural simple ("plátanos").
    `None` si no está: se manda la consulta tal cual (sirve si ya viene en inglés).
    """
    term = normalize_term(query)
    glossary = _glossary()
    for candidate in (term, term.removesuffix("es"), term.removesuffix("s")):
        if candidate in glossary:
            return glossary[candidate], (candidate,)
    return None
