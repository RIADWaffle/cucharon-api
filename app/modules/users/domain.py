"""Lógica pura de users. Sin DB, red ni reloj: el tiempo llega como parámetro."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

MAX_AGE_YEARS = 120


class Sex(StrEnum):
    """Solo para la fórmula de BMR."""

    MALE = "male"
    FEMALE = "female"


class ActivityLevel(StrEnum):
    """Trabajo/rutina SIN contar pasos (los pasos los suma metrics aparte)."""

    SEDENTARY = "sedentary"
    LIGHT = "light"
    MODERATE = "moderate"
    ACTIVE = "active"
    VERY_ACTIVE = "very_active"


class UnitSystem(StrEnum):
    METRIC = "metric"
    IMPERIAL = "imperial"


class RefreshVerdict(StrEnum):
    VALID = "valid"
    EXPIRED = "expired"
    REVOKED = "revoked"
    # Ya se había rotado: alguien está usando un token viejo -> revocar la familia.
    REUSED = "reused"
    # Ya se había rotado, pero hace menos del periodo de gracia y su reemplazo sigue
    # sin usarse (p. ej. el cliente no recibió la respuesta) -> reentregar el reemplazo.
    GRACE = "grace"


@dataclass(frozen=True)
class RefreshTokenState:
    expires_at: datetime
    revoked_at: datetime | None
    replaced_by: UUID | None


@dataclass(frozen=True)
class CurrentUser:
    id: UUID


@dataclass(frozen=True)
class Profile:
    user_id: UUID
    display_name: str | None
    birth_date: date | None
    sex: Sex | None
    height_cm: Decimal | None
    activity_level: ActivityLevel | None
    timezone: str
    unit_system: UnitSystem
    updated_at: datetime


def normalize_email(email: str) -> str:
    return email.strip().lower()


def evaluate_refresh(
    token: RefreshTokenState,
    *,
    now: datetime,
    grace_period: timedelta,
    replacement: RefreshTokenState | None = None,
) -> RefreshVerdict:
    """`replacement` es el token al que apunta `token.replaced_by` (si se rotó)."""
    if token.replaced_by is not None:
        # Al rotar, `revoked_at` marca el momento de la rotación.
        rotated_recently = token.revoked_at is not None and now - token.revoked_at < grace_period
        if (
            rotated_recently
            and replacement is not None
            and evaluate_refresh(replacement, now=now, grace_period=grace_period)
            is RefreshVerdict.VALID
        ):
            return RefreshVerdict.GRACE
        # El reuso se detecta aunque el token viejo ya haya expirado: la familia
        # puede seguir viva y hay que cortarla.
        return RefreshVerdict.REUSED
    if token.revoked_at is not None:
        return RefreshVerdict.REVOKED
    if now >= token.expires_at:
        return RefreshVerdict.EXPIRED
    return RefreshVerdict.VALID


def refresh_expires_at(now: datetime, ttl_days: int) -> datetime:
    return now + timedelta(days=ttl_days)


def is_plausible_birth_date(birth_date: date, today: date) -> bool:
    """No futura y no más de MAX_AGE_YEARS atrás."""
    if birth_date > today:
        return False
    return today.year - birth_date.year <= MAX_AGE_YEARS
