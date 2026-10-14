from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal, Self
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.modules.users.domain import ActivityLevel, Sex, UnitSystem


def _validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Zona horaria IANA desconocida: {value!r}") from exc
    return value


Timezone = Annotated[
    str,
    Field(min_length=1, max_length=64, examples=["America/Mexico_City"]),
    AfterValidator(_validate_timezone),
]
Password = Annotated[str, Field(min_length=8, max_length=128)]
HeightCm = Annotated[Decimal, Field(gt=0, lt=300, max_digits=4, decimal_places=1)]
DisplayName = Annotated[str, Field(min_length=1, max_length=100)]


class RegisterRequest(BaseModel):
    email: EmailStr
    password: Password
    timezone: Timezone


class LoginRequest(BaseModel):
    email: Annotated[str, Field(min_length=1, max_length=320)]
    password: Annotated[str, Field(min_length=1, max_length=128)]


class RefreshRequest(BaseModel):
    refresh_token: Annotated[str, Field(min_length=1, max_length=512)]


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105
    expires_in: int = Field(description="Vida del access token en segundos")


class ProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    display_name: str | None
    birth_date: date | None
    sex: Sex | None
    height_cm: Decimal | None
    activity_level: ActivityLevel | None
    timezone: str
    unit_system: UnitSystem
    updated_at: datetime


class MeResponse(BaseModel):
    id: UUID
    email: str
    created_at: datetime
    profile: ProfileResponse


class ProfileUpdate(BaseModel):
    """PATCH parcial: solo se aplican los campos enviados; `null` borra el valor."""

    model_config = ConfigDict(extra="forbid")

    display_name: DisplayName | None = None
    birth_date: date | None = None
    sex: Sex | None = None
    height_cm: HeightCm | None = None
    activity_level: ActivityLevel | None = None
    timezone: Timezone | None = None
    unit_system: UnitSystem | None = None

    @model_validator(mode="after")
    def _required_fields_not_null(self) -> Self:
        for name in ("timezone", "unit_system"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} no puede ser null")
        return self
