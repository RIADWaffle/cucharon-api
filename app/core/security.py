import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from functools import partial
from uuid import UUID

import anyio
import jwt
from anyio import to_thread
from pwdlib import PasswordHash

from app.core.config import Settings
from app.core.errors import UnauthorizedError

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TYPE = "access"  # noqa: S105  tipo de token, no un secreto

_password_hash = PasswordHash.recommended()
_hash_limiter: anyio.CapacityLimiter | None = None
_dummy_hash: str | None = None


class InvalidTokenError(UnauthorizedError):
    code = "invalid_token"
    default_message = "Token inválido o expirado"


def _limiter(settings: Settings) -> anyio.CapacityLimiter:
    # Se crea perezoso: CapacityLimiter necesita un event loop corriendo.
    global _hash_limiter
    if _hash_limiter is None:
        _hash_limiter = anyio.CapacityLimiter(settings.password_hash_concurrency)
    return _hash_limiter


async def hash_password(password: str, settings: Settings) -> str:
    # argon2 es CPU-bound y bloqueante: va a un thread, con concurrencia limitada.
    return await to_thread.run_sync(_password_hash.hash, password, limiter=_limiter(settings))


async def verify_password(password: str, password_hash: str, settings: Settings) -> bool:
    return await to_thread.run_sync(
        partial(_password_hash.verify, password, password_hash), limiter=_limiter(settings)
    )


async def verify_and_update_password(
    password: str, password_hash: str, settings: Settings
) -> tuple[bool, str | None]:
    """Como `verify_password`, pero si el hash usa parámetros viejos regresa uno nuevo."""
    return await to_thread.run_sync(
        partial(_password_hash.verify_and_update, password, password_hash),
        limiter=_limiter(settings),
    )


async def verify_dummy_password(password: str, settings: Settings) -> None:
    """Gasta lo mismo que un verify real; evita revelar por timing si el email existe."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = await hash_password(secrets.token_urlsafe(16), settings)
    await verify_password(password, _dummy_hash, settings)


def create_access_token(user_id: UUID, now: datetime, settings: Settings) -> str:
    claims = {
        "sub": str(user_id),
        "type": ACCESS_TOKEN_TYPE,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_ttl_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=JWT_ALGORITHM)


def decode_access_token(token: str, settings: Settings) -> UUID:
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "exp", "iat", "type"]},
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError() from exc
    if claims["type"] != ACCESS_TOKEN_TYPE:
        raise InvalidTokenError()
    try:
        return UUID(claims["sub"])
    except ValueError as exc:
        raise InvalidTokenError() from exc


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def derive_rotated_refresh_token(previous: str, settings: Settings) -> str:
    """Sucesor de un refresh token al rotar: HMAC-SHA256(REFRESH_DERIVATION_KEY, anterior).

    Es determinista para poder reentregar el reemplazo dentro del periodo de gracia
    sin guardar tokens en claro: quien presenta el token anterior puede recalcularlo
    junto con el servidor, nadie más. Ver ADR 0002.
    """
    digest = hmac.new(
        settings.refresh_derivation_key.get_secret_value().encode(),
        b"refresh-rotation:" + previous.encode(),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def hash_refresh_token(token: str) -> str:
    # sha256 basta: el token es aleatorio de 256 bits y necesitamos buscarlo por hash.
    return hashlib.sha256(token.encode()).hexdigest()
