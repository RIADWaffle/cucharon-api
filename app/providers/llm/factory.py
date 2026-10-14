from app.core.config import SettingsDep
from app.providers.errors import ProviderNotConfiguredError
from app.providers.llm.base import LLMProvider


def get_llm_provider(settings: SettingsDep) -> LLMProvider:
    """Dependency de FastAPI. Elige la implementación según `LLM_PROVIDER`."""
    # Fase 0: aún no hay implementación real (OpenAICompatibleProvider llega con metrics).
    raise ProviderNotConfiguredError(f"LLM_PROVIDER={settings.llm_provider!r} no soportado")
