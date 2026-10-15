class ProviderError(Exception):
    """Error de un provider externo que NO se debe esconder (config o bug nuestro).

    Las subclases `ProviderUnavailableError` y `ProviderBadDataError` son fallas del
    proveedor y se pueden tolerar (fallback, caché vieja). Cualquier otro
    `ProviderError` sube como 500.
    """


class ProviderNotConfiguredError(ProviderError):
    """La config no selecciona una implementación válida del provider."""


class ProviderUnavailableError(ProviderError):
    """No se pudo obtener respuesta: timeout, red, 5xx, 429 o cuota saliente agotada."""

    def __init__(self, provider: str, reason: str, *, retry_after: float | None = None) -> None:
        self.provider = provider
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(f"{provider}: {reason}")


class ProviderBadDataError(ProviderError):
    """El proveedor respondió, pero con algo que no se puede interpretar."""

    def __init__(self, provider: str, reason: str) -> None:
        self.provider = provider
        self.reason = reason
        super().__init__(f"{provider}: {reason}")
