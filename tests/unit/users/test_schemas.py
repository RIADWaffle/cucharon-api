from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.modules.users.schemas import ProfileUpdate, RegisterRequest


def test_register_requires_timezone() -> None:
    with pytest.raises(ValidationError):
        RegisterRequest.model_validate({"email": "a@x.com", "password": "12345678"})


@pytest.mark.parametrize("tz", ["Mars/Olympus", "", "../../etc/passwd"])
def test_register_rejects_unknown_timezone(tz: str) -> None:
    with pytest.raises(ValidationError):
        RegisterRequest.model_validate({"email": "a@x.com", "password": "12345678", "timezone": tz})


def test_register_accepts_iana_timezone() -> None:
    body = RegisterRequest.model_validate(
        {"email": "a@x.com", "password": "12345678", "timezone": "America/Mexico_City"}
    )
    assert body.timezone == "America/Mexico_City"


def test_register_rejects_short_password() -> None:
    with pytest.raises(ValidationError):
        RegisterRequest.model_validate(
            {"email": "a@x.com", "password": "1234567", "timezone": "UTC"}
        )


def test_profile_update_tracks_only_sent_fields() -> None:
    body = ProfileUpdate.model_validate({"display_name": None, "height_cm": "170.5"})
    assert body.model_dump(exclude_unset=True) == {
        "display_name": None,
        "height_cm": Decimal("170.5"),
    }


@pytest.mark.parametrize("field", ["timezone", "unit_system"])
def test_profile_update_rejects_null_for_required(field: str) -> None:
    with pytest.raises(ValidationError, match="null"):
        ProfileUpdate.model_validate({field: None})


@pytest.mark.parametrize(
    "payload",
    [
        {"height_cm": "0"},
        {"height_cm": "300"},
        {"height_cm": "170.55"},
        {"sex": "other"},
        {"activity_level": "extreme"},
        {"timezone": "Nope/Nope"},
        {"user_id": "x"},
    ],
)
def test_profile_update_rejects_invalid(payload: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        ProfileUpdate.model_validate(payload)
