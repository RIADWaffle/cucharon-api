import httpx
import pytest

pytestmark = pytest.mark.smoke


async def test_health(smoke_client: httpx.AsyncClient) -> None:
    response = await smoke_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready(smoke_client: httpx.AsyncClient) -> None:
    response = await smoke_client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}
