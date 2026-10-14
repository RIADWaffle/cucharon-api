import httpx
from fastapi import FastAPI

from app.core.config import Settings
from app.core.rate_limit import configure_rate_limits

EMAIL = "Ana@Example.com"
PASSWORD = "correct horse battery"


async def _register(client: httpx.AsyncClient, email: str = EMAIL) -> httpx.Response:
    return await client.post(
        "/auth/register",
        json={"email": email, "password": PASSWORD, "timezone": "America/Mexico_City"},
    )


async def _login(client: httpx.AsyncClient, password: str = PASSWORD) -> httpx.Response:
    return await client.post("/auth/login", json={"email": EMAIL, "password": password})


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_full_auth_flow(client: httpx.AsyncClient) -> None:
    # registro
    response = await _register(client)
    assert response.status_code == 201, response.text
    registered = response.json()
    assert registered["email"] == "ana@example.com"
    assert registered["profile"]["timezone"] == "America/Mexico_City"
    assert registered["profile"]["unit_system"] == "metric"

    # login
    response = await _login(client)
    assert response.status_code == 200, response.text
    first = response.json()
    assert first["token_type"] == "bearer"
    assert first["expires_in"] == 15 * 60

    response = await client.get("/me", headers=_bearer(first["access_token"]))
    assert response.status_code == 200
    assert response.json()["id"] == registered["id"]

    # refresh con rotación
    response = await client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert response.status_code == 200, response.text
    second = response.json()
    assert second["refresh_token"] != first["refresh_token"]

    # reintento dentro del periodo de gracia -> mismo reemplazo, sin revocar
    response = await client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert response.status_code == 200, response.text
    assert response.json()["refresh_token"] == second["refresh_token"]

    # se usa el reemplazo; desde ahí, reusar el primero ya es robo
    response = await client.post("/auth/refresh", json={"refresh_token": second["refresh_token"]})
    assert response.status_code == 200, response.text
    third = response.json()

    # reuso del refresh ya rotado -> 401 y se revoca toda la familia (persistida)
    response = await client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_refresh_token"
    response = await client.post("/auth/refresh", json={"refresh_token": third["refresh_token"]})
    assert response.status_code == 401

    # nuevo login y perfil
    response = await _login(client)
    assert response.status_code == 200
    access = response.json()["access_token"]
    response = await client.patch(
        "/me/profile",
        headers=_bearer(access),
        json={
            "display_name": "Ana",
            "birth_date": "1990-05-01",
            "sex": "female",
            "height_cm": "165.5",
            "activity_level": "light",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["height_cm"] == "165.5"

    response = await client.get("/me", headers=_bearer(access))
    profile = response.json()["profile"]
    assert profile["display_name"] == "Ana"
    assert profile["sex"] == "female"
    assert profile["timezone"] == "America/Mexico_City"


async def test_register_duplicate_and_invalid_timezone(client: httpx.AsyncClient) -> None:
    assert (await _register(client)).status_code == 201
    response = await _register(client, email="ana@EXAMPLE.com")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_taken"

    response = await client.post(
        "/auth/register",
        json={"email": "b@example.com", "password": PASSWORD, "timezone": "Mars/Olympus"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_login_error_is_generic(client: httpx.AsyncClient) -> None:
    await _register(client)
    wrong_password = await _login(client, password="nope-nope")
    unknown = await client.post(
        "/auth/login", json={"email": "nadie@example.com", "password": PASSWORD}
    )
    assert wrong_password.status_code == unknown.status_code == 401
    assert wrong_password.json() == unknown.json()


async def test_logout_then_refresh_fails(client: httpx.AsyncClient) -> None:
    await _register(client)
    tokens = (await _login(client)).json()
    response = await client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert response.status_code == 204
    response = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert response.status_code == 401


async def test_me_requires_valid_token(client: httpx.AsyncClient) -> None:
    assert (await client.get("/me")).status_code == 401
    response = await client.get("/me", headers=_bearer("basura"))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"
    assert response.headers["www-authenticate"] == "Bearer"


async def test_patch_profile_rejects_null_timezone(client: httpx.AsyncClient) -> None:
    await _register(client)
    access = (await _login(client)).json()["access_token"]
    response = await client.patch("/me/profile", headers=_bearer(access), json={"timezone": None})
    assert response.status_code == 422


async def test_login_is_rate_limited_by_email(app: FastAPI, client: httpx.AsyncClient) -> None:
    await _register(client)
    # Default: 5/minute por email. Cada intento usa otra IP "spoofeada": no importa.
    for i in range(5):
        response = await client.post(
            "/auth/login",
            json={"email": EMAIL, "password": "nope-nope"},
            headers={"X-Forwarded-For": f"10.0.0.{i}"},
        )
        assert response.status_code == 401
    response = await _login(client)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"


async def test_auth_is_rate_limited_by_ip(
    client: httpx.AsyncClient, test_settings: Settings
) -> None:
    configure_rate_limits(test_settings.model_copy(update={"rate_limit_auth_ip": "3/minute"}))
    try:
        for _ in range(3):
            response = await client.post("/auth/refresh", json={"refresh_token": "x"})
            assert response.status_code == 401
        response = await client.post("/auth/refresh", json={"refresh_token": "x"})
        assert response.status_code == 429
    finally:
        configure_rate_limits(test_settings)
