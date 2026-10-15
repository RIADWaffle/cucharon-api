from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.modules.products.domain import (
    CacheState,
    FetchOrigin,
    NutrientBasis,
    ProductSnapshot,
    SearchCandidate,
    SearchUpsertAction,
    cache_state,
    drop_overridden,
    miss_is_fresh,
    needs_external_search,
    normalize_gtin,
    rank_candidates,
    search_upsert_action,
    source_rank,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


# --- GTIN ---


@pytest.mark.parametrize(
    ("code", "gtin"),
    [
        ("036000291452", "00036000291452"),  # UPC-A
        ("4006381333931", "04006381333931"),  # EAN-13
        ("96385074", "00000096385074"),  # EAN-8
        ("10012345678902", "10012345678902"),  # GTIN-14
        (" 4006381333931 ", "04006381333931"),
    ],
)
def test_normalize_gtin_valid(code: str, gtin: str) -> None:
    assert normalize_gtin(code) == gtin


def test_upc_a_and_its_ean13_form_are_the_same_product() -> None:
    assert normalize_gtin("036000291452") == normalize_gtin("0036000291452")


@pytest.mark.parametrize(
    "code",
    [
        "036000291453",  # dígito verificador incorrecto
        "4006381333930",
        "96385075",
        "",
        "1234567",  # longitud que no es GTIN
        "123456789",
        "123456789012345",
        "40063813339a1",
        "4006381-33393",
        "٤٠٠٦٣٨١٣٣٣٩٣١",  # dígitos no ASCII
    ],
)
def test_normalize_gtin_invalid(code: str) -> None:
    assert normalize_gtin(code) is None


# --- caché ---


def test_cache_state_boundary() -> None:
    ttl = timedelta(days=30)
    assert cache_state(NOW - ttl + timedelta(seconds=1), now=NOW, ttl=ttl) is CacheState.FRESH
    assert cache_state(NOW - ttl, now=NOW, ttl=ttl) is CacheState.STALE


def test_miss_is_fresh_boundary() -> None:
    ttl = timedelta(hours=72)
    assert miss_is_fresh(NOW - timedelta(hours=71), now=NOW, ttl=ttl)
    assert not miss_is_fresh(NOW - ttl, now=NOW, ttl=ttl)


def _snapshot(**changes: object) -> ProductSnapshot:
    base: dict[str, object] = {
        "name": "Avena",
        "brand": "Quaker",
        "basis": NutrientBasis.PER_100G,
        "kcal": Decimal(389),
        "protein_g": Decimal(17),
        "carbs_g": Decimal(56),
        "fat_g": Decimal(7),
        "fiber_g": Decimal(10),
        "sugar_g": Decimal(1),
        "sodium_mg": Decimal(2),
        "serving_quantity": None,
        "serving_unit": None,
        "extra_nutrients": (),
    }
    return ProductSnapshot(**{**base, **changes})  # type: ignore[arg-type]


def test_completeness_counts_core_nutrients() -> None:
    assert _snapshot().completeness == 7
    assert _snapshot(fiber_g=None, sodium_mg=None).completeness == 5


@pytest.mark.parametrize("origin", [FetchOrigin.BARCODE, FetchOrigin.SEARCH])
def test_search_with_same_data_only_touches(origin: FetchOrigin) -> None:
    assert search_upsert_action(_snapshot(), origin, _snapshot()) is SearchUpsertAction.TOUCH


def test_search_never_overwrites_a_barcode_lookup() -> None:
    richer = _snapshot(name="Avena integral")
    action = search_upsert_action(_snapshot(), FetchOrigin.BARCODE, richer)
    assert action is SearchUpsertAction.KEEP


def test_search_does_not_replace_more_complete_data() -> None:
    poorer = _snapshot(fiber_g=None, sugar_g=None)
    assert search_upsert_action(_snapshot(), FetchOrigin.SEARCH, poorer) is SearchUpsertAction.KEEP


@pytest.mark.parametrize(
    "incoming",
    [
        _snapshot(kcal=Decimal(390)),  # igual de completo, datos nuevos
        _snapshot(name="Avena integral"),
    ],
)
def test_search_replaces_equal_or_less_complete_search_data(incoming: ProductSnapshot) -> None:
    assert search_upsert_action(_snapshot(), FetchOrigin.SEARCH, incoming) is (
        SearchUpsertAction.REPLACE
    )


def test_search_fills_a_poorer_search_result() -> None:
    poorer = _snapshot(fiber_g=None, sugar_g=None)
    assert search_upsert_action(poorer, FetchOrigin.SEARCH, _snapshot()) is (
        SearchUpsertAction.REPLACE
    )


# --- ranking ---


def _candidate(name: str, **changes: object) -> SearchCandidate:
    base: dict[str, object] = {
        "kind": "product",
        "id": uuid4(),
        "normalized_name": name,
        "similarity": 0.8,
        "nutrition_complete": True,
        "flagged": False,
        "source": "off",
        "source_type": None,
    }
    return SearchCandidate(**{**base, **changes})  # type: ignore[arg-type]


def _names(candidates: list[SearchCandidate]) -> list[str]:
    return [c.normalized_name for c in candidates]


def test_user_products_rank_first() -> None:
    mine = _candidate("platano mio", kind="user_product", source=None, similarity=0.6)
    exact = _candidate("platano", similarity=1.0)
    assert _names(rank_candidates([exact, mine], "platano")) == ["platano mio", "platano"]


def test_exact_then_prefix_then_similarity() -> None:
    ranked = rank_candidates(
        [
            _candidate("pure de platano", similarity=0.9),
            _candidate("platano macho", similarity=0.7),
            _candidate("platano", similarity=0.6),
        ],
        "platano",
    )
    assert _names(ranked) == ["platano", "platano macho", "pure de platano"]


def test_similar_scores_tie_break_by_quality_then_source() -> None:
    ranked = rank_candidates(
        [
            _candidate("a platano", similarity=0.81, flagged=True),
            _candidate("b platano", similarity=0.80, nutrition_complete=False),
            _candidate("c platano", similarity=0.79, source="usda", source_type="Branded"),
            _candidate("d platano", similarity=0.80, source="usda", source_type="Foundation"),
        ],
        "platano",
    )
    assert _names(ranked) == ["d platano", "c platano", "a platano", "b platano"]


@pytest.mark.parametrize(
    ("source", "source_type", "rank"),
    [
        ("usda", "Foundation", 0),
        ("usda", "SR Legacy", 1),
        ("usda", "Survey (FNDDS)", 2),
        ("off", None, 3),
        ("usda", "Branded", 4),
        ("usda", "Experimental", 5),
        (None, None, 5),
    ],
)
def test_source_rank(source: str | None, source_type: str | None, rank: int) -> None:
    assert source_rank(source, source_type) == rank


def test_needs_external_search() -> None:
    assert needs_external_search(3, 20)
    assert not needs_external_search(20, 20)


def test_override_replaces_global_product() -> None:
    base = _candidate("platano")
    override = _candidate("platano", kind="user_product", id=uuid4(), source=None)
    other = _candidate("platano macho")
    result = drop_overridden([base, override, other], {base.id})
    assert result == [override, other]
