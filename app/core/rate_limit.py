from collections.abc import Awaitable, Callable
from typing import cast

from limits import parse_many
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import Settings
from app.core.errors import RateLimitedError

# Límite por IP (decorador `@limiter.limit(auth_ip_limit)` en las rutas). OJO: detrás
# del proxy de Render uvicorn corre con `--forwarded-allow-ips='*'` y toma la entrada
# MÁS A LA IZQUIERDA de X-Forwarded-For, que el cliente controla: este límite es
# spoofeable. Por eso login se limita además por email (`KeyedRateLimiter`). Ver ADR 0003.
# Almacenamiento en memoria: válido con una sola instancia (Render free).
limiter = Limiter(key_func=get_remote_address)

_auth_ip_limit = "20/minute"


def auth_ip_limit() -> str:
    """Límite dinámico para `@limiter.limit`; se fija en `configure_rate_limits`."""
    return _auth_ip_limit


def rate_limit_auth_ip[**P, R](func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """`@limiter.limit(auth_ip_limit)` tipado. La ruta debe recibir `request: Request`."""
    return cast(Callable[P, Awaitable[R]], limiter.limit(auth_ip_limit)(func))


def configure_rate_limits(settings: Settings) -> None:
    global _auth_ip_limit
    _auth_ip_limit = settings.rate_limit_auth_ip
    limiter.enabled = settings.rate_limit_enabled


class KeyedRateLimiter:
    """Rate limit por una llave arbitraria (p. ej. el email del login)."""

    def __init__(self, limits: str, *, enabled: bool = True) -> None:
        self._items = parse_many(limits)
        self._storage = MemoryStorage()
        self._strategy = MovingWindowRateLimiter(self._storage)
        self.enabled = enabled

    def hit(self, namespace: str, key: str) -> None:
        if not self.enabled:
            return
        for item in self._items:
            if not self._strategy.hit(item, namespace, key):
                raise RateLimitedError()

    def reset(self) -> None:
        self._storage.reset()
