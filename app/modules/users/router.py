from typing import Any

from fastapi import APIRouter, Request, status

from app.core.config import SettingsDep
from app.core.db import SessionDep
from app.core.errors import ErrorResponse
from app.core.rate_limit import rate_limit_auth_ip
from app.modules.users import service
from app.modules.users.dependencies import CurrentUserDep, LoginEmailLimiterDep
from app.modules.users.domain import normalize_email
from app.modules.users.schemas import (
    LoginRequest,
    MeResponse,
    ProfileResponse,
    ProfileUpdate,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
)
from app.modules.users.service import Me, TokenPair

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
}

auth_router = APIRouter(prefix="/auth", tags=["auth"], responses=_ERRORS)
me_router = APIRouter(prefix="/me", tags=["me"], responses=_ERRORS)


def _token_response(pair: TokenPair) -> TokenResponse:
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
    )


def _me_response(me: Me) -> MeResponse:
    return MeResponse(
        id=me.id,
        email=me.email,
        created_at=me.created_at,
        profile=ProfileResponse.model_validate(me.profile),
    )


@auth_router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorResponse}},
)
@rate_limit_auth_ip
async def register(
    request: Request, body: RegisterRequest, session: SessionDep, settings: SettingsDep
) -> MeResponse:
    me = await service.register(
        session,
        email=body.email,
        password=body.password,
        timezone=body.timezone,
        settings=settings,
    )
    return _me_response(me)


@auth_router.post("/login")
@rate_limit_auth_ip
async def login(
    request: Request,
    body: LoginRequest,
    session: SessionDep,
    settings: SettingsDep,
    email_limiter: LoginEmailLimiterDep,
) -> TokenResponse:
    # Por email además de por IP: la IP detrás del proxy es spoofeable. Ver ADR 0003.
    email_limiter.hit("login", normalize_email(body.email))
    pair = await service.login(session, email=body.email, password=body.password, settings=settings)
    return _token_response(pair)


@auth_router.post("/refresh")
@rate_limit_auth_ip
async def refresh(
    request: Request, body: RefreshRequest, session: SessionDep, settings: SettingsDep
) -> TokenResponse:
    pair = await service.refresh(session, refresh_token=body.refresh_token, settings=settings)
    return _token_response(pair)


@auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
@rate_limit_auth_ip
async def logout(request: Request, body: RefreshRequest, session: SessionDep) -> None:
    await service.logout(session, refresh_token=body.refresh_token)


@me_router.get("")
async def get_me(user: CurrentUserDep, session: SessionDep) -> MeResponse:
    return _me_response(await service.get_me(session, user.id))


@me_router.patch("/profile", responses={404: {"model": ErrorResponse}})
async def update_profile(
    body: ProfileUpdate, user: CurrentUserDep, session: SessionDep
) -> ProfileResponse:
    profile = await service.update_profile(session, user.id, body.model_dump(exclude_unset=True))
    return ProfileResponse.model_validate(profile)


router = APIRouter()
router.include_router(auth_router)
router.include_router(me_router)
