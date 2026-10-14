import pytest

from app.core.errors import (
    ConflictError,
    DomainError,
    DomainValidationError,
    ForbiddenError,
    NotFoundError,
    RateLimitedError,
    UnauthorizedError,
    status_for,
)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (NotFoundError(), 404),
        (ConflictError(), 409),
        (UnauthorizedError(), 401),
        (ForbiddenError(), 403),
        (DomainValidationError(), 422),
        (RateLimitedError(), 429),
        (DomainError(), 400),
    ],
)
def test_status_for(exc: DomainError, expected: int) -> None:
    assert status_for(exc) == expected


def test_subclass_inherits_status_and_overrides_code() -> None:
    class EmailTaken(ConflictError):
        code = "email_taken"

    exc = EmailTaken("ya existe")
    assert status_for(exc) == 409
    assert exc.code == "email_taken"
    assert exc.message == "ya existe"


def test_default_message() -> None:
    assert NotFoundError().message == "Recurso no encontrado"
