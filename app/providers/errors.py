class ProviderError(Exception):
    """Error de un provider externo (red, respuesta inválida, cuota...)."""


class ProviderNotConfiguredError(ProviderError):
    """La config no selecciona una implementación válida del provider."""
