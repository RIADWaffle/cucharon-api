import os
from collections.abc import AsyncIterator

import httpx
import pytest

# Render free duerme la instancia: el primer request puede tardar hasta ~90 s.
COLD_START_TIMEOUT_SECONDS = 90
REQUEST_TIMEOUT_SECONDS = 15


@pytest.fixture(scope="session")
async def smoke_client() -> AsyncIterator[httpx.AsyncClient]:
    base_url = os.environ.get("SMOKE_BASE_URL")
    if not base_url:
        pytest.skip("SMOKE_BASE_URL no está definido")
    async with httpx.AsyncClient(base_url=base_url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        # Calentamiento: despierta la instancia antes de los tests reales.
        await client.get("/health", timeout=COLD_START_TIMEOUT_SECONDS)
        yield client
