from decimal import Decimal

import pytest

from app.providers.food_data import Nutrients, QualityFlag, assess_quality
from app.providers.food_data.normalize import (
    clean_text,
    estimated_kcal,
    kj_to_kcal,
    salt_to_sodium_mg,
    to_decimal,
)

D = Decimal
# Avena: 4*17 + 4*56 + 9*7 + 2*10 = 375 kcal, coherente con 389.
GOOD = Nutrients(
    kcal=D(389),
    protein_g=D(17),
    carbs_g=D(56),
    fat_g=D(7),
    fiber_g=D(10),
    sugar_g=D(1),
    sodium_mg=D(2),
)


def _with(**changes: Decimal | None) -> Nutrients:
    return Nutrients(**{**GOOD.__dict__, **changes})


def test_good_data_has_no_flags() -> None:
    assert assess_quality(GOOD, "per_100g") == ()


@pytest.mark.parametrize(
    ("nutrients", "flag"),
    [
        (_with(fat_g=D(-1)), QualityFlag.NEGATIVE_VALUE),
        (_with(kcal=D(950)), QualityFlag.ENERGY_OUT_OF_RANGE),
        (_with(sodium_mg=D(40_001)), QualityFlag.SODIUM_OUT_OF_RANGE),
        (_with(carbs_g=D(80), fat_g=D(20)), QualityFlag.MACROS_EXCEED_BASIS),
        (_with(kcal=D(100)), QualityFlag.ENERGY_MISMATCH),
        (_with(sugar_g=D(58)), QualityFlag.SUGAR_EXCEEDS_CARBS),
    ],
)
def test_suspicious_data_is_flagged(nutrients: Nutrients, flag: QualityFlag) -> None:
    assert flag in assess_quality(nutrients, "per_100g")


def test_flags_mark_but_never_correct() -> None:
    bad = _with(kcal=D(100))
    assess_quality(bad, "per_100g")
    assert bad.kcal == D(100)


def test_mass_check_only_applies_per_100g() -> None:
    syrup = Nutrients(kcal=D(430), protein_g=D(0), carbs_g=D(107), fat_g=D(0))
    assert QualityFlag.MACROS_EXCEED_BASIS in assess_quality(syrup, "per_100g")
    assert QualityFlag.MACROS_EXCEED_BASIS not in assess_quality(syrup, "per_100ml")


def test_energy_check_counts_alcohol_and_skips_without_macros() -> None:
    beer = Nutrients(kcal=D(43), protein_g=D("0.5"), carbs_g=D("3.6"), fat_g=D(0))
    assert QualityFlag.ENERGY_MISMATCH in assess_quality(beer, "per_100ml")
    assert assess_quality(beer, "per_100ml", alcohol_g=D("3.9")) == ()
    assert estimated_kcal(Nutrients(kcal=D(43))) is None
    assert assess_quality(Nutrients(kcal=D(43)), "per_100ml") == ()


def test_energy_tolerance_has_absolute_floor() -> None:
    # 10 kcal de diferencia en algo de 20 kcal: dentro del piso de 15 kcal.
    light = Nutrients(kcal=D(20), protein_g=D(0), carbs_g=D("7.5"), fat_g=D(0))
    assert assess_quality(light, "per_100ml") == ()


def test_conversions() -> None:
    assert kj_to_kcal(D(2252)) == D("538.2")
    assert salt_to_sodium_mg(D("0.107")) == D("42.8")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (6.3, D("6.3")),
        (0, D(0)),
        ("12.5", D("12.5")),
        (" 3 ", D(3)),
        ("n/a", None),
        (None, None),
        (True, None),
        (float("nan"), None),
        ([1], None),
    ],
)
def test_to_decimal(raw: object, expected: Decimal | None) -> None:
    assert to_decimal(raw) == expected


def test_clean_text() -> None:
    assert clean_text("  Pepsi   Max ") == "Pepsi Max"
    assert clean_text("   ") is None
    assert clean_text(3) is None
