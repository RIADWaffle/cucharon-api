from pydantic_settings import SettingsConfigDict

from app.core.config import Settings


class IsolatedSettings(Settings):
    """Settings que ignoran el `.env` local del desarrollador."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")
