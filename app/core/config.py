from functools import lru_cache
from typing import Annotated, Any, Literal, Self
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Depends, Request
from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Parámetros de libpq que asyncpg no entiende como query string.
_LIBPQ_ONLY_PARAMS = {"sslmode", "channel_binding"}


def normalize_database_url(url: str) -> tuple[str, dict[str, Any]]:
    """Convierte una URL estilo libpq (la que da Neon) a una URL de SQLAlchemy+asyncpg.

    Regresa `(url, connect_args)`. `sslmode` pasa a `connect_args["ssl"]` y
    `channel_binding` se descarta porque asyncpg no lo acepta en la URL.
    """
    parts = urlsplit(url)
    scheme = parts.scheme
    if scheme in ("postgres", "postgresql"):
        scheme = "postgresql+asyncpg"
    if scheme != "postgresql+asyncpg":
        raise ValueError(f"DATABASE_URL debe ser postgresql://, no {parts.scheme}://")

    query = parse_qsl(parts.query, keep_blank_values=True)
    connect_args: dict[str, Any] = {}
    kept: list[tuple[str, str]] = []
    for key, value in query:
        if key == "sslmode":
            if value != "disable":
                connect_args["ssl"] = value
        elif key not in _LIBPQ_ONLY_PARAMS:
            kept.append((key, value))

    normalized = urlunsplit((scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))
    return normalized, connect_args


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["local", "test", "production"] = "local"
    log_level: str = "INFO"
    log_json: bool = True

    # Neon: usa la URL DIRECTA (host sin "-pooler"). El pooler de Neon es PgBouncer en
    # modo transaction: rompe el caché de prepared statements de asyncpg y los
    # advisory locks/DDL de Alembic. El pool lo maneja SQLAlchemy.
    database_url: str
    db_pool_size: int = 5
    db_max_overflow: int = 5

    # Firma los access tokens (JWT HS256).
    jwt_secret: SecretStr
    # Deriva el sucesor de un refresh token al rotar (HMAC). Llave distinta de
    # `jwt_secret` (separación de llaves): cada una se rota sin afectar a la otra.
    # Ver ADR 0002.
    refresh_derivation_key: SecretStr
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30
    # Reusar un refresh ya rotado dentro de esta ventana (y con su reemplazo sin usar)
    # regresa ese reemplazo en vez de revocar la familia. 0 = sin gracia. Ver ADR 0002.
    refresh_token_grace_seconds: int = 30

    # argon2 usa ~64 MiB y CPU por hash; limita cuántos corren a la vez.
    password_hash_concurrency: int = 2

    rate_limit_enabled: bool = True
    # Sintaxis de `limits`: "N/periodo", varias separadas por ";".
    rate_limit_auth_ip: str = "20/minute"
    rate_limit_login_email: str = "5/minute;20/hour"

    llm_provider: str | None = None
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None

    food_data_provider: str | None = None

    storage_provider: Literal["local"] = "local"
    storage_local_dir: str = ".storage"
    storage_local_base_url: str = "http://localhost:8000/_dev/storage"
    storage_local_secret: SecretStr | None = None

    @field_validator("database_url")
    @classmethod
    def _reject_neon_pooler(cls, value: str) -> str:
        host = urlsplit(value).hostname or ""
        if "-pooler" in host:
            raise ValueError("Usa la URL directa de Neon (sin '-pooler'); ver config.py")
        normalize_database_url(value)
        return value

    @model_validator(mode="after")
    def _require_distinct_keys(self) -> Self:
        if self.refresh_derivation_key.get_secret_value() == self.jwt_secret.get_secret_value():
            raise ValueError("REFRESH_DERIVATION_KEY debe ser distinta de JWT_SECRET")
        return self

    @property
    def sqlalchemy_database_url(self) -> tuple[str, dict[str, Any]]:
        return normalize_database_url(self.database_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]


def get_app_settings(request: Request) -> Settings:
    """Settings con los que se creó la app (`create_app`)."""
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
