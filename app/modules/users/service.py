from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.config import Settings
from app.modules.users.domain import (
    Profile,
    RefreshTokenState,
    RefreshVerdict,
    evaluate_refresh,
    is_plausible_birth_date,
    normalize_email,
    refresh_expires_at,
)
from app.modules.users.exceptions import (
    EmailAlreadyRegisteredError,
    InactiveUserError,
    InvalidBirthDateError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    ProfileNotFoundError,
    RefreshTokenReusedError,
)
from app.modules.users.models import RefreshToken, User, UserProfile


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int


@dataclass(frozen=True)
class Me:
    id: UUID
    email: str
    created_at: datetime
    profile: Profile


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _to_profile(model: UserProfile) -> Profile:
    return Profile(
        user_id=model.user_id,
        display_name=model.display_name,
        birth_date=model.birth_date,
        sex=model.sex,
        height_cm=model.height_cm,
        activity_level=model.activity_level,
        timezone=model.timezone,
        unit_system=model.unit_system,
        updated_at=model.updated_at,
    )


async def register(
    session: AsyncSession, *, email: str, password: str, timezone: str, settings: Settings
) -> Me:
    email = normalize_email(email)
    if await session.scalar(select(User.id).where(User.email == email)) is not None:
        raise EmailAlreadyRegisteredError()

    user = User(email=email, password_hash=await security.hash_password(password, settings))
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:  # carrera: otro registro con el mismo email
        raise EmailAlreadyRegisteredError() from exc

    profile = UserProfile(user_id=user.id, timezone=timezone)
    session.add(profile)
    await session.flush()
    return Me(
        id=user.id, email=user.email, created_at=user.created_at, profile=_to_profile(profile)
    )


async def login(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    settings: Settings,
    now: datetime | None = None,
) -> TokenPair:
    now = now or _utcnow()
    user = await session.scalar(select(User).where(User.email == normalize_email(email)))
    if user is None:
        await security.verify_dummy_password(password, settings)
        raise InvalidCredentialsError()
    valid, new_hash = await security.verify_and_update_password(
        password, user.password_hash, settings
    )
    if not valid:
        raise InvalidCredentialsError()
    if not user.is_active:
        raise InvalidCredentialsError()
    if new_hash is not None:
        # El hash usa parámetros de argon2 viejos: se actualiza con el password en mano.
        user.password_hash = new_hash
    pair, _ = await _issue_tokens(
        session, user.id, uuid4(), security.generate_refresh_token(), settings, now
    )
    return pair


async def refresh(
    session: AsyncSession,
    *,
    refresh_token: str,
    settings: Settings,
    now: datetime | None = None,
) -> TokenPair:
    now = now or _utcnow()
    # FOR UPDATE: dos refresh simultáneos con el mismo token se serializan; el
    # segundo ve el token ya rotado y cae en el periodo de gracia. Ver ADR 0002.
    token = await _get_token_for_update(
        session, RefreshToken.token_hash == security.hash_refresh_token(refresh_token)
    )
    if token is None:
        raise InvalidRefreshTokenError()
    replacement = (
        await _get_token_for_update(session, RefreshToken.id == token.replaced_by)
        if token.replaced_by is not None
        else None
    )

    verdict = evaluate_refresh(
        _state(token),
        now=now,
        grace_period=timedelta(seconds=settings.refresh_token_grace_seconds),
        replacement=_state(replacement) if replacement is not None else None,
    )
    if verdict is RefreshVerdict.REUSED:
        await _revoke_family(session, token.family_id, now)
        # Única excepción a "los services no hacen commit": la revocación debe
        # persistir aunque la respuesta sea 401 (get_session hará rollback por la
        # excepción, ya sin nada pendiente). Ver ADR 0002.
        await session.commit()
        raise RefreshTokenReusedError()
    if verdict not in (RefreshVerdict.VALID, RefreshVerdict.GRACE):
        raise InvalidRefreshTokenError()

    is_active = await session.scalar(select(User.is_active).where(User.id == token.user_id))
    if not is_active:
        raise InvalidRefreshTokenError()

    successor = security.derive_rotated_refresh_token(refresh_token, settings)
    if verdict is RefreshVerdict.GRACE and replacement is not None:  # GRACE implica reemplazo
        if security.hash_refresh_token(successor) != replacement.token_hash:
            # REFRESH_DERIVATION_KEY cambió desde la rotación: no se puede reentregar. No es
            # señal de robo, así que no se revoca la familia.
            raise InvalidRefreshTokenError()
        return _token_pair(token.user_id, successor, settings, now)

    pair, new_token_id = await _issue_tokens(
        session, token.user_id, token.family_id, successor, settings, now
    )
    token.revoked_at = now
    token.replaced_by = new_token_id
    await session.flush()
    return pair


async def logout(session: AsyncSession, *, refresh_token: str, now: datetime | None = None) -> None:
    """Revoca toda la familia del token. Idempotente: un token desconocido no es error."""
    now = now or _utcnow()
    family_id = await session.scalar(
        select(RefreshToken.family_id).where(
            RefreshToken.token_hash == security.hash_refresh_token(refresh_token)
        )
    )
    if family_id is not None:
        await _revoke_family(session, family_id, now)


async def get_me(session: AsyncSession, user_id: UUID) -> Me:
    row = (
        await session.execute(
            select(User, UserProfile)
            .join(UserProfile, UserProfile.user_id == User.id)
            .where(User.id == user_id)
        )
    ).one_or_none()
    if row is None:
        raise InactiveUserError()
    user, profile = row
    if not user.is_active:
        raise InactiveUserError()
    return Me(
        id=user.id, email=user.email, created_at=user.created_at, profile=_to_profile(profile)
    )


async def get_profile(session: AsyncSession, user_id: UUID) -> Profile:
    """API pública: la usan otros módulos (p. ej. metrics para BMR y timezone)."""
    return _to_profile(await _get_profile_model(session, user_id))


async def update_profile(
    session: AsyncSession,
    user_id: UUID,
    changes: dict[str, Any],
    *,
    now: datetime | None = None,
) -> Profile:
    """`changes` solo trae los campos enviados (ya validados por el schema)."""
    now = now or _utcnow()
    birth_date = changes.get("birth_date")
    if birth_date is not None and not is_plausible_birth_date(birth_date, now.date()):
        raise InvalidBirthDateError()

    profile = await _get_profile_model(session, user_id)
    for field, value in changes.items():
        setattr(profile, field, value)
    await session.flush()
    return _to_profile(profile)


async def _get_profile_model(session: AsyncSession, user_id: UUID) -> UserProfile:
    profile = await session.get(UserProfile, user_id)
    if profile is None:
        raise ProfileNotFoundError()
    return profile


async def _get_token_for_update(
    session: AsyncSession, criterion: ColumnElement[bool]
) -> RefreshToken | None:
    return await session.scalar(select(RefreshToken).where(criterion).with_for_update())


def _state(token: RefreshToken) -> RefreshTokenState:
    return RefreshTokenState(
        expires_at=token.expires_at, revoked_at=token.revoked_at, replaced_by=token.replaced_by
    )


async def _issue_tokens(
    session: AsyncSession,
    user_id: UUID,
    family_id: UUID,
    raw_refresh: str,
    settings: Settings,
    now: datetime,
) -> tuple[TokenPair, UUID]:
    token = RefreshToken(
        id=uuid4(),
        user_id=user_id,
        family_id=family_id,
        token_hash=security.hash_refresh_token(raw_refresh),
        expires_at=refresh_expires_at(now, settings.refresh_token_ttl_days),
    )
    session.add(token)
    await session.flush()
    return _token_pair(user_id, raw_refresh, settings, now), token.id


def _token_pair(user_id: UUID, raw_refresh: str, settings: Settings, now: datetime) -> TokenPair:
    return TokenPair(
        access_token=security.create_access_token(user_id, now, settings),
        refresh_token=raw_refresh,
        expires_in=settings.access_token_ttl_minutes * 60,
    )


async def _revoke_family(session: AsyncSession, family_id: UUID, now: datetime) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
