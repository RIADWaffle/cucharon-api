"""Requests JSON hacia un proveedor. Único lugar que toca httpx en food_data.

Traduce todo a nuestras excepciones; ninguna excepción de httpx sale de aquí.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import anyio
import httpx
import structlog

from app.providers.errors import ProviderBadDataError, ProviderError, ProviderUnavailableError
from app.providers.food_data.budget import OutboundBudget

logger = structlog.get_logger(__name__)

# Si el proveedor pide frenar (429/503) sin decir cuánto.
DEFAULT_PAUSE_SECONDS = 60.0


@dataclass(frozen=True)
class JsonResponse:
    status_code: int
    body: Any


async def request_json(
    client: httpx.AsyncClient,
    *,
    method: str = "GET",
    provider: str,
    kind: str,
    budget: OutboundBudget,
    url: str,
    max_seconds: float,
    params: Mapping[str, str | int] | None = None,
    headers: Mapping[str, str] | None = None,
    json_body: Mapping[str, Any] | None = None,
) -> JsonResponse:
    """Regresa 2xx y 404 (con el cuerpo JSON, o `None` si no es JSON en un 404).

    - Timeout, red, 5xx, 429 -> `ProviderUnavailableError`.
    - 4xx distinto de 404/429 con cuerpo JSON -> `ProviderError`: la API rechazó la
      request (parámetro inválido, key inválida); es un bug o mala config nuestra y
      no se debe esconder con un fallback.
    - 4xx con cuerpo NO JSON (página de un proxy/nginx) -> `ProviderUnavailableError`:
      USDA devuelve 400 intermitentes desde nginx para requests válidas (ADR 0006).
    - 2xx que no es JSON -> `ProviderBadDataError`.
    """
    budget.acquire(kind)
    try:
        with anyio.fail_after(max_seconds):
            response = await client.request(
                method, url, params=params, headers=headers, json=json_body
            )
    except TimeoutError as exc:
        raise ProviderUnavailableError(provider, "timeout") from exc
    except httpx.TransportError as exc:  # incluye los timeouts propios de httpx
        raise ProviderUnavailableError(provider, f"red: {type(exc).__name__}") from exc
    except httpx.HTTPError as exc:
        raise ProviderError(f"{provider}: {type(exc).__name__}") from exc

    status = response.status_code
    if status in (429, 503):
        retry_after = _retry_after(response) or DEFAULT_PAUSE_SECONDS
        budget.pause(kind, retry_after)
        logger.warning("provider_throttled", provider=provider, status=status, pause=retry_after)
        raise ProviderUnavailableError(provider, f"HTTP {status}", retry_after=retry_after)
    if status >= 500:
        raise ProviderUnavailableError(provider, f"HTTP {status}")

    try:
        body = response.json()
    except ValueError as exc:
        if status == 404:
            return JsonResponse(status, None)
        if status >= 400:
            logger.error(
                "provider_non_json_error",
                provider=provider,
                kind=kind,
                status=status,
                # Sin la key aunque viniera en la URL (USDA la acepta como query param).
                url=str(response.request.url.copy_remove_param("api_key")),
            )
            raise ProviderUnavailableError(provider, f"HTTP {status} sin JSON") from exc
        raise ProviderBadDataError(provider, "respuesta no es JSON") from exc
    if status >= 400 and status != 404:
        raise ProviderError(f"{provider}: HTTP {status} en {kind}: {str(body)[:200]}")
    return JsonResponse(status, body)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None  # formato fecha HTTP: se usa la pausa default
    return seconds if seconds > 0 else None
