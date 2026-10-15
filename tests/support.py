import json
from pathlib import Path
from typing import Any

from pydantic_settings import SettingsConfigDict

from app.core.config import Settings


class IsolatedSettings(Settings):
    """Settings que ignoran el `.env` local del desarrollador."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")


FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(relative: str) -> Any:
    """JSON capturado de una API real y recortado (ver tests/fixtures/README.md)."""
    return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))
