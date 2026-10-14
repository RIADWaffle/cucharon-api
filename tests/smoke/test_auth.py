import os

import httpx
import pytest

pytestmark = pytest.mark.smoke


async def test_login_and_me(smoke_client: httpx.AsyncClient) -> None:
    """Usuario de prueba dedicado; solo lectura (aparte del refresh token del login)."""
    email = os.environ.get("SMOKE_USER_EMAIL")
    password = os.environ.get("SMOKE_USER_PASSWORD")
    if not email or not password:
        pytest.skip("SMOKE_USER_EMAIL / SMOKE_USER_PASSWORD no definidos")

    response = await smoke_client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    tokens = response.json()

    response = await smoke_client.get(
        "/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert response.status_code == 200
    assert response.json()["email"] == email.lower()

    await smoke_client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]})
