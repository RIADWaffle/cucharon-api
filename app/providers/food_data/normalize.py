"""Normalización y chequeos de calidad compartidos por los providers (puros).

Los datos sospechosos se MARCAN con `QualityFlag`, nunca se corrigen. Ver ADR 0006.
"""

from decimal import Decimal, InvalidOperation
from enum import StrEnum

from app.providers.food_data.base import NutrientBasis, Nutrients

KJ_PER_KCAL = Decimal("4.184")
SODIUM_PER_SALT = Decimal("2.5")  # sal = sodio * 2.5
KCAL_QUANTUM = Decimal("0.1")

# Factores de Atwater generales (kcal/g). Carbohidratos DISPONIBLES (sin fibra).
KCAL_PER_G_PROTEIN = Decimal(4)
KCAL_PER_G_CARBS = Decimal(4)
KCAL_PER_G_FAT = Decimal(9)
KCAL_PER_G_FIBER = Decimal(2)
KCAL_PER_G_ALCOHOL = Decimal(7)

# Umbrales: generosos a propósito; solo deben saltar con datos claramente malos.
MAX_KCAL_PER_100 = Decimal(920)  # grasa pura ~900
MAX_SODIUM_MG_PER_100 = Decimal(40_000)  # sal pura ~39 300 mg de sodio
MAX_MASS_PER_100G = Decimal(102)  # P + C + G + fibra en 100 g, con tolerancia de redondeo
ENERGY_TOLERANCE_KCAL = Decimal(15)
ENERGY_TOLERANCE_RATIO = Decimal("0.20")
SUGAR_TOLERANCE_G = Decimal(1)


class QualityFlag(StrEnum):
    NEGATIVE_VALUE = "negative_value"
    ENERGY_OUT_OF_RANGE = "energy_out_of_range"
    SODIUM_OUT_OF_RANGE = "sodium_out_of_range"
    MACROS_EXCEED_BASIS = "macros_exceed_basis"
    ENERGY_MISMATCH = "energy_mismatch"
    SUGAR_EXCEEDS_CARBS = "sugar_exceeds_carbs"
    # La fuente no dice si es por 100 g o 100 ml y parece bebida.
    BASIS_UNCERTAIN = "basis_uncertain"
    # USDA sin fibra: no se pudo restar de "carbohydrate by difference".
    CARBS_MAY_INCLUDE_FIBER = "carbs_may_include_fiber"
    # Un valor no cabe en la columna (absurdo); se guarda como desconocido.
    VALUE_OUT_OF_RANGE = "value_out_of_range"


def to_decimal(value: object) -> Decimal | None:
    """Número de una respuesta JSON a `Decimal`. Lo que no es número finito -> `None`."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float | str):
        try:
            result = Decimal(str(value).strip())
        except InvalidOperation:
            return None
        return result if result.is_finite() else None
    return None


def kj_to_kcal(kj: Decimal) -> Decimal:
    return (kj / KJ_PER_KCAL).quantize(KCAL_QUANTUM)


def salt_to_sodium_mg(salt_g: Decimal) -> Decimal:
    return salt_g / SODIUM_PER_SALT * 1000


def estimated_kcal(nutrients: Nutrients, alcohol_g: Decimal | None = None) -> Decimal | None:
    """Atwater con carbohidratos disponibles. `None` si falta una macro."""
    if nutrients.protein_g is None or nutrients.carbs_g is None or nutrients.fat_g is None:
        return None
    return (
        KCAL_PER_G_PROTEIN * nutrients.protein_g
        + KCAL_PER_G_CARBS * nutrients.carbs_g
        + KCAL_PER_G_FAT * nutrients.fat_g
        + KCAL_PER_G_FIBER * (nutrients.fiber_g or 0)
        + KCAL_PER_G_ALCOHOL * (alcohol_g or 0)
    )


def assess_quality(
    nutrients: Nutrients, basis: NutrientBasis, *, alcohol_g: Decimal | None = None
) -> tuple[QualityFlag, ...]:
    """Banderas de datos sospechosos, en orden estable."""
    flags: list[QualityFlag] = []
    values = [
        nutrients.kcal,
        nutrients.protein_g,
        nutrients.carbs_g,
        nutrients.fat_g,
        nutrients.fiber_g,
        nutrients.sugar_g,
        nutrients.sodium_mg,
    ]
    if any(v is not None and v < 0 for v in values):
        flags.append(QualityFlag.NEGATIVE_VALUE)
    if nutrients.kcal is not None and nutrients.kcal > MAX_KCAL_PER_100:
        flags.append(QualityFlag.ENERGY_OUT_OF_RANGE)
    if nutrients.sodium_mg is not None and nutrients.sodium_mg > MAX_SODIUM_MG_PER_100:
        flags.append(QualityFlag.SODIUM_OUT_OF_RANGE)

    # En líquidos la masa de 100 ml puede pasar de 100 g (jarabes): solo aplica a g.
    mass = [nutrients.protein_g, nutrients.carbs_g, nutrients.fat_g, nutrients.fiber_g]
    if basis == "per_100g" and sum(v for v in mass if v is not None) > MAX_MASS_PER_100G:
        flags.append(QualityFlag.MACROS_EXCEED_BASIS)

    estimate = estimated_kcal(nutrients, alcohol_g)
    if nutrients.kcal is not None and estimate is not None:
        tolerance = max(
            ENERGY_TOLERANCE_KCAL, ENERGY_TOLERANCE_RATIO * max(nutrients.kcal, estimate)
        )
        if abs(nutrients.kcal - estimate) > tolerance:
            flags.append(QualityFlag.ENERGY_MISMATCH)

    if (
        nutrients.sugar_g is not None
        and nutrients.carbs_g is not None
        and nutrients.sugar_g > nutrients.carbs_g + SUGAR_TOLERANCE_G
    ):
        flags.append(QualityFlag.SUGAR_EXCEEDS_CARBS)
    return tuple(flags)


def clean_text(value: object) -> str | None:
    """Texto no vacío y sin espacios sobrantes; cualquier otra cosa -> `None`."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text or None
