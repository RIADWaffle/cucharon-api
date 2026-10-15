import time
from collections.abc import Callable, Mapping

from limits import RateLimitItem, parse_many
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter

from app.providers.errors import ProviderUnavailableError


class OutboundBudget:
    """Cuota saliente hacia un proveedor, por tipo de request ("product", "search"...).

    Todas las requests salen de la misma IP, así que la cuota del proveedor es
    compartida por todos los usuarios. Además, si el proveedor responde 429/503, se
    pausa ese tipo de request hasta su `Retry-After` para no arriesgar un ban.
    En memoria: válido con una sola instancia. Ver ADR 0006.
    """

    def __init__(
        self,
        provider: str,
        limits: Mapping[str, str],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._provider = provider
        self._items: dict[str, list[RateLimitItem]] = {
            kind: parse_many(value) for kind, value in limits.items()
        }
        self._storage = MemoryStorage()
        self._strategy = MovingWindowRateLimiter(self._storage)
        self._paused_until: dict[str, float] = {}
        self._clock = clock

    def acquire(self, kind: str) -> None:
        now = self._clock()
        paused_until = self._paused_until.get(kind, 0.0)
        if now < paused_until:
            raise ProviderUnavailableError(
                self._provider, f"{kind} en pausa", retry_after=paused_until - now
            )
        for item in self._items[kind]:
            if not self._strategy.test(item, self._provider, kind):
                stats = self._strategy.get_window_stats(item, self._provider, kind)
                raise ProviderUnavailableError(
                    self._provider,
                    f"cuota saliente de {kind} agotada",
                    retry_after=max(stats.reset_time - now, 1.0),
                )
        for item in self._items[kind]:
            self._strategy.hit(item, self._provider, kind)

    def pause(self, kind: str, seconds: float) -> None:
        until = self._clock() + seconds
        self._paused_until[kind] = max(self._paused_until.get(kind, 0.0), until)

    def reset(self) -> None:
        self._storage.reset()
        self._paused_until.clear()
