from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from pydantic import SecretStr

from app.core import security
from app.core.config import Settings
from app.core.security import InvalidTokenError

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_access_token_roundtrip(test_settings: Settings) -> None:
    user_id = uuid4()
    token = security.create_access_token(user_id, datetime.now(UTC), test_settings)
    assert security.decode_access_token(token, test_settings) == user_id


def test_expired_access_token_is_rejected(test_settings: Settings) -> None:
    token = security.create_access_token(uuid4(), NOW - timedelta(days=1), test_settings)
    with pytest.raises(InvalidTokenError):
        security.decode_access_token(token, test_settings)


def test_token_signed_with_other_secret_is_rejected(test_settings: Settings) -> None:
    other_secret = SecretStr("other-secret-0123456789abcdef0123456789")
    other = test_settings.model_copy(update={"jwt_secret": other_secret})
    token = security.create_access_token(uuid4(), datetime.now(UTC), other)
    with pytest.raises(InvalidTokenError):
        security.decode_access_token(token, test_settings)


def _encode(claims: Mapping[str, object], settings: Settings) -> str:
    return jwt.encode(dict(claims), settings.jwt_secret.get_secret_value(), algorithm="HS256")


def test_token_with_wrong_type_is_rejected(test_settings: Settings) -> None:
    now = datetime.now(UTC)
    claims = {"sub": str(uuid4()), "type": "refresh", "iat": now, "exp": now + timedelta(minutes=5)}
    with pytest.raises(InvalidTokenError):
        security.decode_access_token(_encode(claims, test_settings), test_settings)


def test_token_with_non_uuid_sub_is_rejected(test_settings: Settings) -> None:
    now = datetime.now(UTC)
    claims = {"sub": "nope", "type": "access", "iat": now, "exp": now + timedelta(minutes=5)}
    with pytest.raises(InvalidTokenError):
        security.decode_access_token(_encode(claims, test_settings), test_settings)


def test_token_without_required_claims_is_rejected(test_settings: Settings) -> None:
    with pytest.raises(InvalidTokenError):
        security.decode_access_token(_encode({"sub": str(uuid4())}, test_settings), test_settings)


def test_refresh_token_hash_is_deterministic_and_not_plaintext() -> None:
    token = security.generate_refresh_token()
    assert security.hash_refresh_token(token) == security.hash_refresh_token(token)
    assert security.hash_refresh_token(token) != token
    assert security.generate_refresh_token() != token


async def test_password_hash_and_verify(test_settings: Settings) -> None:
    hashed = await security.hash_password("correct horse", test_settings)
    assert hashed.startswith("$argon2")
    assert await security.verify_password("correct horse", hashed, test_settings)
    assert not await security.verify_password("wrong", hashed, test_settings)


async def test_verify_dummy_password_runs(test_settings: Settings) -> None:
    await security.verify_dummy_password("anything", test_settings)


async def test_verify_and_update_rehashes_outdated_params(test_settings: Settings) -> None:
    weak = PasswordHash((Argon2Hasher(time_cost=1, memory_cost=8 * 1024, parallelism=1),))
    outdated = weak.hash("correct horse")

    valid, new_hash = await security.verify_and_update_password(
        "correct horse", outdated, test_settings
    )
    assert valid
    assert new_hash is not None
    assert new_hash != outdated
    assert await security.verify_and_update_password("correct horse", new_hash, test_settings) == (
        True,
        None,
    )
    assert await security.verify_and_update_password("wrong", outdated, test_settings) == (
        False,
        None,
    )


def test_rotated_refresh_token_is_deterministic_and_keyed(test_settings: Settings) -> None:
    previous = security.generate_refresh_token()
    successor = security.derive_rotated_refresh_token(previous, test_settings)
    assert successor == security.derive_rotated_refresh_token(previous, test_settings)
    assert successor != previous
    assert len(successor) == len(previous)  # mismo formato que token_urlsafe(32)

    other_key = test_settings.model_copy(
        update={"refresh_derivation_key": SecretStr("otra-llave-0123456789abcdef0123456789")}
    )
    assert security.derive_rotated_refresh_token(previous, other_key) != successor

    # Separación de llaves: rotar JWT_SECRET no cambia la derivación.
    other_jwt = test_settings.model_copy(
        update={"jwt_secret": SecretStr("otro-secreto-0123456789abcdef0123456789")}
    )
    assert security.derive_rotated_refresh_token(previous, other_jwt) == successor
