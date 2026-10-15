from typing import get_args

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings, normalize_database_url
from tests.support import IsolatedSettings


def _settings(database_url: str) -> Settings:
    return IsolatedSettings(
        database_url=database_url,
        jwt_secret=SecretStr("x"),
        refresh_derivation_key=SecretStr("y"),
    )


def test_normalize_neon_url_moves_sslmode_to_connect_args() -> None:
    url, connect_args = normalize_database_url(
        "postgresql://u:p@ep-cool-123.us-east-2.aws.neon.tech/db?sslmode=require&channel_binding=require"
    )
    assert url == "postgresql+asyncpg://u:p@ep-cool-123.us-east-2.aws.neon.tech/db"
    assert connect_args == {"ssl": "require"}


def test_normalize_keeps_other_params_and_asyncpg_scheme() -> None:
    url, connect_args = normalize_database_url(
        "postgresql+asyncpg://u:p@localhost/db?sslmode=disable&application_name=api"
    )
    assert url == "postgresql+asyncpg://u:p@localhost/db?application_name=api"
    assert connect_args == {}


def test_normalize_accepts_postgres_scheme() -> None:
    url, _ = normalize_database_url("postgres://u:p@localhost/db")
    assert url.startswith("postgresql+asyncpg://")


def test_normalize_rejects_other_drivers() -> None:
    with pytest.raises(ValueError, match="postgresql"):
        normalize_database_url("mysql://u:p@localhost/db")


def test_settings_reject_neon_pooler_host() -> None:
    with pytest.raises(ValidationError, match="pooler"):
        _settings("postgresql://u:p@ep-cool-123-pooler.us-east-2.aws.neon.tech/db")


def test_settings_accept_direct_neon_host() -> None:
    settings = _settings("postgresql://u:p@ep-cool-123.us-east-2.aws.neon.tech/db?sslmode=require")
    url, connect_args = settings.sqlalchemy_database_url
    assert url.startswith("postgresql+asyncpg://")
    assert connect_args == {"ssl": "require"}


def test_settings_reject_reused_key_for_refresh_derivation() -> None:
    with pytest.raises(ValidationError, match="REFRESH_DERIVATION_KEY"):
        IsolatedSettings(
            database_url="postgresql://u:p@localhost/db",
            jwt_secret=SecretStr("same"),
            refresh_derivation_key=SecretStr("same"),
        )


def _food_settings(**fields: object) -> Settings:
    return IsolatedSettings(
        database_url="postgresql://u:p@localhost/db",
        jwt_secret=SecretStr("x"),
        refresh_derivation_key=SecretStr("y"),
        **fields,  # pyright: ignore[reportArgumentType]
    )


def test_empty_food_data_providers_only_allowed_in_local_and_test() -> None:
    # Allowlist: todo ENV fuera de {local, test} exige providers.
    for env in get_args(Settings.model_fields["env"].annotation):
        if env in ("local", "test"):
            assert _food_settings(env=env, food_data_providers="").food_data_providers == []
        else:
            with pytest.raises(ValidationError, match="FOOD_DATA_PROVIDERS"):
                _food_settings(env=env, food_data_providers="")
    ok = _food_settings(
        env="production", food_data_providers="off", food_data_contact_email="a@b.c"
    )
    assert ok.food_data_providers == ["off"]


def test_unknown_env_fails_at_startup() -> None:
    with pytest.raises(ValidationError):
        _food_settings(env="staging")


def test_off_requires_contact_email() -> None:
    with pytest.raises(ValidationError, match="FOOD_DATA_CONTACT_EMAIL"):
        _food_settings(food_data_providers="off,usda")


def test_demo_key_only_in_local() -> None:
    assert _food_settings(usda_api_key=SecretStr("DEMO_KEY")).usda_api_key is not None
    with pytest.raises(ValidationError, match="DEMO_KEY"):
        _food_settings(env="test", usda_api_key=SecretStr("DEMO_KEY"))
