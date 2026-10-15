from functools import lru_cache
from typing import Annotated, Any, Literal, Self
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Depends, Request
from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

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


_ENVS_WITHOUT_FOOD_DATA = frozenset({"local", "test"})


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

    # --- food data (ADR 0006) ---
    # Orden de la cadena, separado por comas ("off,usda"). Vacío = sin providers
    # externos: solo caché local y productos del usuario.
    food_data_providers: Annotated[list[Literal["off", "usda"]], NoDecode] = []
    # OFF exige un User-Agent "AppName/Version (ContactEmail)".
    food_data_contact_email: str | None = None
    # Tiempo máximo total de una llamada a un provider (connect + lectura + cola).
    food_data_timeout_seconds: float = 8.0
    off_base_url: str = "https://world.openfoodfacts.org"
    # Cuotas SALIENTES (todas las requests salen de la misma IP). Límites oficiales
    # de OFF: 15/min lecturas de producto, 10/min búsquedas.
    off_rate_limit_product: str = "15/minute"
    off_rate_limit_search: str = "10/minute"
    usda_base_url: str = "https://api.nal.usda.gov/fdc/v1"
    # Sin key, USDA queda desactivado. DEMO_KEY solo con ENV=local.
    usda_api_key: SecretStr | None = None
    # Oficial: 1000/hora por key, y excederlo bloquea la key 1 h. Se deja margen.
    usda_rate_limit: str = "900/hour"
    products_cache_ttl_days: int = 30
    products_miss_ttl_hours: int = 72
    # Por usuario, en los endpoints que pueden pegarle a una API externa.
    rate_limit_products_barcode: str = "30/minute"
    rate_limit_products_search: str = "15/minute"

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

    @field_validator("food_data_providers", mode="before")
    @classmethod
    def _split_providers(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @model_validator(mode="after")
    def _validate_food_data(self) -> Self:
        # Sin providers el escaneo de códigos nuevos siempre da 503. Allowlist: solo
        # desarrollo y la suite (que usa fakes); cualquier otro ENV falla al arrancar.
        if not self.food_data_providers and self.env not in _ENVS_WITHOUT_FOOD_DATA:
            raise ValueError(f"FOOD_DATA_PROVIDERS no puede estar vacío con ENV={self.env}")
        if "off" in self.food_data_providers and not self.food_data_contact_email:
            raise ValueError("FOOD_DATA_CONTACT_EMAIL es obligatorio con el provider 'off'")
        key = self.usda_api_key.get_secret_value() if self.usda_api_key else None
        if key == "DEMO_KEY" and self.env != "local":
            raise ValueError("USDA_API_KEY=DEMO_KEY solo se permite con ENV=local")
        return self

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
