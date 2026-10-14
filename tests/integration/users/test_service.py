from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import (
    decode_access_token,
    hash_refresh_token,
    verify_and_update_password,
)
from app.modules.users import service
from app.modules.users.domain import ActivityLevel, Sex, UnitSystem
from app.modules.users.exceptions import (
    EmailAlreadyRegisteredError,
    InactiveUserError,
    InvalidBirthDateError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    ProfileNotFoundError,
    RefreshTokenReusedError,
)
from app.modules.users.models import RefreshToken, User

PASSWORD = "correct horse battery"


async def _register(
    session: AsyncSession, settings: Settings, email: str = "Ana@Example.com"
) -> service.Me:
    return await service.register(
        session, email=email, password=PASSWORD, timezone="America/Mexico_City", settings=settings
    )


async def _login(
    session: AsyncSession, settings: Settings, now: datetime | None = None
) -> service.TokenPair:
    return await service.login(
        session, email="ana@example.com", password=PASSWORD, settings=settings, now=now
    )


def _past() -> datetime:
    """Base para tests con `now` explícito; en el pasado para que `iat` no quede adelantado."""
    return datetime.now(UTC) - timedelta(minutes=1)


async def _token(session: AsyncSession, raw: str) -> RefreshToken:
    token = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw))
    )
    assert token is not None
    return token


async def test_register_creates_user_and_profile(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    me = await _register(db_session, test_settings)
    assert me.email == "ana@example.com"
    assert me.profile.timezone == "America/Mexico_City"
    assert me.profile.unit_system is UnitSystem.METRIC
    assert me.profile.display_name is None
    user = await db_session.get(User, me.id)
    assert user is not None
    assert user.password_hash.startswith("$argon2")


async def test_register_duplicate_email_is_case_insensitive(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    await _register(db_session, test_settings)
    with pytest.raises(EmailAlreadyRegisteredError):
        await _register(db_session, test_settings, email="ANA@example.com")


async def test_db_rejects_non_lowercase_email(db_session: AsyncSession) -> None:
    db_session.add(User(email="Ana@x.com", password_hash="x"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_login_issues_tokens(db_session: AsyncSession, test_settings: Settings) -> None:
    me = await _register(db_session, test_settings)
    pair = await _login(db_session, test_settings)
    assert decode_access_token(pair.access_token, test_settings) == me.id
    assert pair.expires_in == test_settings.access_token_ttl_minutes * 60
    token = await _token(db_session, pair.refresh_token)
    assert token.user_id == me.id
    assert token.token_hash != pair.refresh_token


async def test_login_errors_are_generic(db_session: AsyncSession, test_settings: Settings) -> None:
    await _register(db_session, test_settings)
    with pytest.raises(InvalidCredentialsError) as wrong_password:
        await service.login(
            db_session, email="ana@example.com", password="nope-nope", settings=test_settings
        )
    with pytest.raises(InvalidCredentialsError) as unknown_email:
        await service.login(
            db_session, email="nadie@example.com", password=PASSWORD, settings=test_settings
        )
    assert wrong_password.value.message == unknown_email.value.message


async def test_login_rejects_inactive_user(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    me = await _register(db_session, test_settings)
    await db_session.execute(update(User).where(User.id == me.id).values(is_active=False))
    with pytest.raises(InvalidCredentialsError):
        await _login(db_session, test_settings)


async def test_login_rehashes_outdated_password_hash(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    me = await _register(db_session, test_settings)
    weak = PasswordHash((Argon2Hasher(time_cost=1, memory_cost=8 * 1024, parallelism=1),))
    outdated = weak.hash(PASSWORD)
    await db_session.execute(update(User).where(User.id == me.id).values(password_hash=outdated))

    await _login(db_session, test_settings)

    stored = await db_session.scalar(select(User.password_hash).where(User.id == me.id))
    assert stored is not None
    assert stored != outdated
    assert await verify_and_update_password(PASSWORD, stored, test_settings) == (True, None)


async def test_login_keeps_current_password_hash(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    me = await _register(db_session, test_settings)
    before = await db_session.scalar(select(User.password_hash).where(User.id == me.id))
    await _login(db_session, test_settings)
    after = await db_session.scalar(select(User.password_hash).where(User.id == me.id))
    assert after == before


async def test_refresh_rotates_within_family(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    await _register(db_session, test_settings)
    first = await _login(db_session, test_settings)
    second = await service.refresh(
        db_session, refresh_token=first.refresh_token, settings=test_settings
    )

    old = await _token(db_session, first.refresh_token)
    new = await _token(db_session, second.refresh_token)
    assert old.revoked_at is not None
    assert old.replaced_by == new.id
    assert new.family_id == old.family_id
    assert new.revoked_at is None


async def test_reuse_outside_grace_revokes_whole_family(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    await _register(db_session, test_settings)
    t0 = _past()
    first = await _login(db_session, test_settings, now=t0)
    other_device = await _login(db_session, test_settings, now=t0)
    second = await service.refresh(
        db_session, refresh_token=first.refresh_token, settings=test_settings, now=t0
    )

    later = t0 + timedelta(seconds=test_settings.refresh_token_grace_seconds)
    with pytest.raises(RefreshTokenReusedError):
        await service.refresh(
            db_session, refresh_token=first.refresh_token, settings=test_settings, now=later
        )
    with pytest.raises(InvalidRefreshTokenError):
        await service.refresh(
            db_session, refresh_token=second.refresh_token, settings=test_settings, now=later
        )
    # Otra familia (otro login/dispositivo) no se toca.
    assert (await _token(db_session, other_device.refresh_token)).revoked_at is None


async def test_reuse_within_grace_returns_same_replacement(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    me = await _register(db_session, test_settings)
    t0 = _past()
    first = await _login(db_session, test_settings, now=t0)
    second = await service.refresh(
        db_session, refresh_token=first.refresh_token, settings=test_settings, now=t0
    )

    # p. ej. la respuesta del primer refresh se perdió y el cliente reintenta
    retry = await service.refresh(
        db_session,
        refresh_token=first.refresh_token,
        settings=test_settings,
        now=t0 + timedelta(seconds=10),
    )
    assert retry.refresh_token == second.refresh_token
    assert decode_access_token(retry.access_token, test_settings) == me.id
    assert (await _token(db_session, second.refresh_token)).revoked_at is None
    # el reemplazo sigue siendo la cabeza de la familia y rota normal
    third = await service.refresh(
        db_session,
        refresh_token=second.refresh_token,
        settings=test_settings,
        now=t0 + timedelta(seconds=15),
    )
    assert third.refresh_token != second.refresh_token


async def test_reuse_within_grace_after_replacement_was_used_is_reuse(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    await _register(db_session, test_settings)
    t0 = _past()
    first = await _login(db_session, test_settings, now=t0)
    second = await service.refresh(
        db_session, refresh_token=first.refresh_token, settings=test_settings, now=t0
    )
    third = await service.refresh(
        db_session,
        refresh_token=second.refresh_token,
        settings=test_settings,
        now=t0 + timedelta(seconds=5),
    )

    with pytest.raises(RefreshTokenReusedError):
        await service.refresh(
            db_session,
            refresh_token=first.refresh_token,
            settings=test_settings,
            now=t0 + timedelta(seconds=10),
        )
    assert (await _token(db_session, third.refresh_token)).revoked_at is not None


async def test_grace_replay_after_derivation_key_change_is_rejected_without_revoking(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    await _register(db_session, test_settings)
    t0 = _past()
    first = await _login(db_session, test_settings, now=t0)
    second = await service.refresh(
        db_session, refresh_token=first.refresh_token, settings=test_settings, now=t0
    )
    rotated_key = test_settings.model_copy(
        update={"refresh_derivation_key": SecretStr("otra-llave-0123456789abcdef0123456789")}
    )

    with pytest.raises(InvalidRefreshTokenError) as exc_info:
        await service.refresh(
            db_session,
            refresh_token=first.refresh_token,
            settings=rotated_key,
            now=t0 + timedelta(seconds=5),
        )
    assert not isinstance(exc_info.value, RefreshTokenReusedError)
    assert (await _token(db_session, second.refresh_token)).revoked_at is None


async def test_grace_replay_survives_jwt_secret_change(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    me = await _register(db_session, test_settings)
    t0 = _past()
    first = await _login(db_session, test_settings, now=t0)
    second = await service.refresh(
        db_session, refresh_token=first.refresh_token, settings=test_settings, now=t0
    )
    rotated_jwt = test_settings.model_copy(
        update={"jwt_secret": SecretStr("otro-secreto-0123456789abcdef0123456789")}
    )

    retry = await service.refresh(
        db_session,
        refresh_token=first.refresh_token,
        settings=rotated_jwt,
        now=t0 + timedelta(seconds=5),
    )
    assert retry.refresh_token == second.refresh_token
    # el access token nuevo ya va firmado con el secreto nuevo
    assert decode_access_token(retry.access_token, rotated_jwt) == me.id


async def test_refresh_rejects_unknown_and_expired(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    await _register(db_session, test_settings)
    past = datetime.now(UTC) - timedelta(days=test_settings.refresh_token_ttl_days + 1)
    stale = await _login(db_session, test_settings, now=past)
    with pytest.raises(InvalidRefreshTokenError):
        await service.refresh(db_session, refresh_token="no-existe", settings=test_settings)
    with pytest.raises(InvalidRefreshTokenError) as exc_info:
        await service.refresh(db_session, refresh_token=stale.refresh_token, settings=test_settings)
    assert not isinstance(exc_info.value, RefreshTokenReusedError)


async def test_refresh_rejects_inactive_user(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    me = await _register(db_session, test_settings)
    pair = await _login(db_session, test_settings)
    await db_session.execute(update(User).where(User.id == me.id).values(is_active=False))
    with pytest.raises(InvalidRefreshTokenError):
        await service.refresh(db_session, refresh_token=pair.refresh_token, settings=test_settings)


async def test_logout_revokes_family_and_is_idempotent(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    await _register(db_session, test_settings)
    pair = await _login(db_session, test_settings)
    await service.logout(db_session, refresh_token=pair.refresh_token)
    await service.logout(db_session, refresh_token=pair.refresh_token)
    await service.logout(db_session, refresh_token="desconocido")
    with pytest.raises(InvalidRefreshTokenError):
        await service.refresh(db_session, refresh_token=pair.refresh_token, settings=test_settings)


async def test_update_profile_partial_and_nullable(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    me = await _register(db_session, test_settings)
    profile = await service.update_profile(
        db_session,
        me.id,
        {
            "display_name": "Ana",
            "birth_date": date(1990, 5, 1),
            "sex": Sex.FEMALE,
            "height_cm": Decimal("165.5"),
            "activity_level": ActivityLevel.LIGHT,
            "unit_system": UnitSystem.IMPERIAL,
        },
    )
    assert profile.height_cm == Decimal("165.5")
    assert profile.timezone == "America/Mexico_City"

    profile = await service.update_profile(db_session, me.id, {"display_name": None})
    assert profile.display_name is None
    assert profile.sex is Sex.FEMALE
    assert await service.get_profile(db_session, me.id) == profile


async def test_update_profile_rejects_future_birth_date(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    me = await _register(db_session, test_settings)
    now = datetime(2026, 6, 1, tzinfo=UTC)
    with pytest.raises(InvalidBirthDateError):
        await service.update_profile(db_session, me.id, {"birth_date": date(2026, 6, 2)}, now=now)


async def test_get_profile_missing(db_session: AsyncSession) -> None:
    with pytest.raises(ProfileNotFoundError):
        await service.get_profile(db_session, uuid4())


async def test_get_me_missing_or_inactive(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    with pytest.raises(InactiveUserError):
        await service.get_me(db_session, uuid4())
    me = await _register(db_session, test_settings)
    await db_session.execute(update(User).where(User.id == me.id).values(is_active=False))
    with pytest.raises(InactiveUserError):
        await service.get_me(db_session, me.id)
