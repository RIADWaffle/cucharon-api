from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import SettingsDep
from app.core.rate_limit import KeyedRateLimiter
from app.core.security import InvalidTokenError, decode_access_token
from app.modules.users.domain import CurrentUser

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> CurrentUser:
    """Solo valida el JWT (sin DB). `is_active` se revisa en login y refresh."""
    if credentials is None:
        raise InvalidTokenError("Falta el header Authorization: Bearer <token>")
    return CurrentUser(id=decode_access_token(credentials.credentials, settings))


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


def get_login_email_limiter(request: Request) -> KeyedRateLimiter:
    return request.app.state.login_email_limiter


LoginEmailLimiterDep = Annotated[KeyedRateLimiter, Depends(get_login_email_limiter)]
