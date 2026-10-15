"""Lógica pura de products. Sin DB, red ni reloj: el tiempo llega como parámetro."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID

GTIN_LENGTHS = frozenset({8, 12, 13, 14})
GTIN_CANONICAL_LENGTH = 14

OFF_ATTRIBUTION_TEXT = "Datos de Open Food Facts, disponibles bajo la Open Database License"
ODBL_URL = "https://opendatacommons.org/licenses/odbl/1-0/"

# Coincidencia "fuerte" en búsqueda local; igual al default de
# pg_trgm.word_similarity_threshold que usa el operador `<%`.
STRONG_MATCH = 0.6


class ProductSource(StrEnum):
    OFF = "off"
    USDA = "usda"


class NutrientBasis(StrEnum):
    PER_100G = "per_100g"
    PER_100ML = "per_100ml"


class ServingUnit(StrEnum):
    G = "g"
    ML = "ml"


class FetchOrigin(StrEnum):
    """Cómo llegó el registro a la caché. Un lookup por barcode trae el producto
    completo y es autoritativo; una búsqueda trae resultados parciales."""

    BARCODE = "barcode"
    SEARCH = "search"


class CacheState(StrEnum):
    FRESH = "fresh"
    STALE = "stale"


class SearchUpsertAction(StrEnum):
    REPLACE = "replace"
    # Mismos datos: solo se actualiza `fetched_at`.
    TOUCH = "touch"
    # No se toca: el existente es más completo o vino de un lookup por barcode.
    KEEP = "keep"


# --- códigos de barras (GS1) ---


def normalize_gtin(code: str) -> str | None:
    """UPC-A, EAN-8, EAN-13 o GTIN-14 -> GTIN-14 (14 dígitos con ceros a la izquierda).

    `None` si no son solo dígitos, la longitud no es de un GTIN o el dígito
    verificador no cuadra. UPC-E no se acepta: el cliente lo expande a UPC-A.
    """
    code = code.strip()
    if not code.isascii() or not code.isdigit() or len(code) not in GTIN_LENGTHS:
        return None
    if not _check_digit_ok(code):
        return None
    return code.zfill(GTIN_CANONICAL_LENGTH)


def _check_digit_ok(code: str) -> bool:
    # Pesos 3,1,3,1... desde el dígito anterior al verificador, de derecha a izquierda.
    body = [int(c) for c in reversed(code[:-1])]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(body))
    return (10 - total % 10) % 10 == int(code[-1])


# --- caché ---


def cache_state(fetched_at: datetime, *, now: datetime, ttl: timedelta) -> CacheState:
    return CacheState.FRESH if now - fetched_at < ttl else CacheState.STALE


def miss_is_fresh(checked_at: datetime, *, now: datetime, ttl: timedelta) -> bool:
    return now - checked_at < ttl


@dataclass(frozen=True)
class ProductSnapshot:
    """Lo que la fuente dice de un producto (sin ids ni fechas), para compararlo."""

    name: str
    brand: str | None
    basis: NutrientBasis
    kcal: Decimal | None
    protein_g: Decimal | None
    carbs_g: Decimal | None
    fat_g: Decimal | None
    fiber_g: Decimal | None
    sugar_g: Decimal | None
    sodium_mg: Decimal | None
    serving_quantity: Decimal | None
    serving_unit: ServingUnit | None
    extra_nutrients: tuple[tuple[str, Decimal], ...]

    @property
    def completeness(self) -> int:
        """Cuántos de los 7 nutrientes principales trae."""
        values = (
            self.kcal,
            self.protein_g,
            self.carbs_g,
            self.fat_g,
            self.fiber_g,
            self.sugar_g,
            self.sodium_mg,
        )
        return sum(v is not None for v in values)


def search_upsert_action(
    existing: ProductSnapshot, existing_origin: FetchOrigin, incoming: ProductSnapshot
) -> SearchUpsertAction:
    """Una búsqueda NUNCA degrada la caché. Ver ADR 0006.

    - Trae lo mismo -> TOUCH (solo `fetched_at`).
    - El existente vino de un lookup por barcode -> KEEP.
    - El existente tiene más nutrientes -> KEEP.
    - Si no -> REPLACE.
    """
    if existing == incoming:
        return SearchUpsertAction.TOUCH
    if existing_origin is FetchOrigin.BARCODE:
        return SearchUpsertAction.KEEP
    if existing.completeness > incoming.completeness:
        return SearchUpsertAction.KEEP
    return SearchUpsertAction.REPLACE


# --- ranking de búsqueda ---

CandidateKind = Literal["user_product", "product"]

# Menor = mejor. Genéricos de USDA arriba; Branded de USDA al final (OFF cubre marcas).
_SOURCE_RANK = {
    ("usda", "Foundation"): 0,
    ("usda", "SR Legacy"): 1,
    ("usda", "Survey (FNDDS)"): 2,
    ("off", None): 3,
    ("usda", "Branded"): 4,
}
_UNKNOWN_SOURCE_RANK = 5


@dataclass(frozen=True)
class SearchCandidate:
    kind: CandidateKind
    id: UUID
    # Texto normalizado (minúsculas, sin acentos) del nombre.
    normalized_name: str
    similarity: float
    nutrition_complete: bool
    flagged: bool
    source: str | None = None  # None para productos del usuario
    source_type: str | None = None


def source_rank(source: str | None, source_type: str | None) -> int:
    if source == "off":
        return _SOURCE_RANK[("off", None)]
    return _SOURCE_RANK.get((source or "", source_type), _UNKNOWN_SOURCE_RANK)


def _match_rank(candidate: SearchCandidate, query: str) -> int:
    if candidate.normalized_name == query:
        return 0
    if candidate.normalized_name.startswith(query):
        return 1
    return 2


def rank_candidates(
    candidates: Iterable[SearchCandidate], normalized_query: str
) -> list[SearchCandidate]:
    """Orden final de la búsqueda:

    1. Productos del usuario (custom y overrides) antes que la caché global.
    2. Coincidencia exacta, luego prefijo, luego el resto.
    3. Similitud (pg_trgm `word_similarity`), redondeada a 0.05 para que las
       siguientes reglas desempaten entre parecidos.
    4. Datos completos y sin banderas de calidad.
    5. Fuente (ver `_SOURCE_RANK`).
    6. Nombre, para que el orden sea estable.
    """

    def key(c: SearchCandidate) -> tuple[int, int, float, int, int, int, str]:
        return (
            0 if c.kind == "user_product" else 1,
            _match_rank(c, normalized_query),
            -round(c.similarity * 20) / 20,
            0 if c.nutrition_complete else 1,
            1 if c.flagged else 0,
            source_rank(c.source, c.source_type),
            c.normalized_name,
        )

    return sorted(candidates, key=key)


def needs_external_search(strong_local_matches: int, limit: int) -> bool:
    """Solo se sale a OFF/USDA si lo local no alcanza: la cuota de OFF es mínima."""
    return strong_local_matches < limit


def drop_overridden(
    candidates: Sequence[SearchCandidate], overridden_product_ids: set[UUID]
) -> list[SearchCandidate]:
    """El override del usuario reemplaza al producto global en los resultados."""
    return [c for c in candidates if not (c.kind == "product" and c.id in overridden_product_ids)]
