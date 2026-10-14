from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.modules.users.domain import (
    RefreshTokenState,
    RefreshVerdict,
    evaluate_refresh,
    is_plausible_birth_date,
    normalize_email,
    refresh_expires_at,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=1)


def test_normalize_email_lowercases_and_strips() -> None:
    assert normalize_email("  Ana.Perez@Example.COM ") == "ana.perez@example.com"


GRACE = timedelta(seconds=30)


def _state(
    *,
    expires_at: datetime = LATER,
    revoked_at: datetime | None = None,
    replaced_by: UUID | None = None,
) -> RefreshTokenState:
    return RefreshTokenState(expires_at=expires_at, revoked_at=revoked_at, replaced_by=replaced_by)


def _rotated(rotated_at: datetime = NOW) -> RefreshTokenState:
    return _state(revoked_at=rotated_at, replaced_by=uuid4())


def test_refresh_valid() -> None:
    assert evaluate_refresh(_state(), now=NOW, grace_period=GRACE) is RefreshVerdict.VALID


def test_refresh_expired_at_exact_boundary() -> None:
    verdict = evaluate_refresh(_state(expires_at=NOW), now=NOW, grace_period=GRACE)
    assert verdict is RefreshVerdict.EXPIRED


def test_refresh_revoked_without_replacement() -> None:
    verdict = evaluate_refresh(_state(revoked_at=NOW), now=NOW, grace_period=GRACE)
    assert verdict is RefreshVerdict.REVOKED


def test_rotated_token_within_grace_with_unused_replacement() -> None:
    verdict = evaluate_refresh(
        _rotated(), now=NOW + timedelta(seconds=29), grace_period=GRACE, replacement=_state()
    )
    assert verdict is RefreshVerdict.GRACE


@pytest.mark.parametrize(
    ("now", "grace_period", "replacement"),
    [
        pytest.param(NOW + GRACE, GRACE, _state(), id="window-boundary"),
        pytest.param(NOW + timedelta(minutes=5), GRACE, _state(), id="outside-window"),
        pytest.param(NOW, timedelta(0), _state(), id="grace-disabled"),
        pytest.param(NOW, GRACE, None, id="replacement-missing"),
        pytest.param(NOW, GRACE, _rotated(), id="replacement-already-used"),
        pytest.param(NOW, GRACE, _state(revoked_at=NOW), id="replacement-revoked"),
        pytest.param(NOW, GRACE, _state(expires_at=NOW), id="replacement-expired"),
    ],
)
def test_rotated_token_is_reuse(
    now: datetime, grace_period: timedelta, replacement: RefreshTokenState | None
) -> None:
    verdict = evaluate_refresh(
        _rotated(), now=now, grace_period=grace_period, replacement=replacement
    )
    assert verdict is RefreshVerdict.REUSED


def test_rotated_without_rotation_time_is_reuse() -> None:
    token = _state(replaced_by=uuid4())
    verdict = evaluate_refresh(token, now=NOW, grace_period=GRACE, replacement=_state())
    assert verdict is RefreshVerdict.REUSED


def test_reuse_wins_over_expiry() -> None:
    token = _state(expires_at=NOW - timedelta(days=1), revoked_at=NOW, replaced_by=uuid4())
    verdict = evaluate_refresh(token, now=NOW + timedelta(minutes=5), grace_period=GRACE)
    assert verdict is RefreshVerdict.REUSED


def test_refresh_expires_at() -> None:
    assert refresh_expires_at(NOW, 30) == NOW + timedelta(days=30)


@pytest.mark.parametrize(
    ("birth_date", "expected"),
    [
        (date(1990, 5, 1), True),
        (date(2026, 6, 1), True),
        (date(2026, 6, 2), False),
        (date(1906, 1, 1), True),
        (date(1905, 12, 31), False),
    ],
)
def test_is_plausible_birth_date(birth_date: date, expected: bool) -> None:
    assert is_plausible_birth_date(birth_date, date(2026, 6, 1)) is expected
