"""Contract tests contra las APIs REALES. No corren en CI (`-m "not external"`).

Validan la FORMA de las respuestas (lo que el parser necesita), no valores
nutricionales exactos: esos cambian. Leen `USDA_API_KEY` y
`FOOD_DATA_CONTACT_EMAIL` del entorno o del `.env`.
"""

from collections.abc import AsyncIterator

import httpx
import pytest

from app.core.config import Settings, get_settings
from app.core.http import create_http_client


@pytest.fixture(scope="session")
def real_settings() -> Settings:
    settings = get_settings()
    if not settings.food_data_contact_email:
        # OFF exige un contacto en el User-Agent; uno genérico para pruebas manuales.
        settings = settings.model_copy(
            update={"food_data_contact_email": "contract-tests@cucharon.invalid"}
        )
    return settings


@pytest.fixture
async def http_client(real_settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    async with create_http_client(real_settings) as client:
        yield client
