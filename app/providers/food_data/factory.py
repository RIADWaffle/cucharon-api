from app.core.config import SettingsDep
from app.providers.errors import ProviderNotConfiguredError
from app.providers.food_data.base import FoodDataProvider


def get_food_data_provider(settings: SettingsDep) -> FoodDataProvider:
    """Dependency de FastAPI. Elige la implementación según `FOOD_DATA_PROVIDER`."""
    # Fase 0: aún no hay implementación real (OFF/USDA llegan con products).
    raise ProviderNotConfiguredError(
        f"FOOD_DATA_PROVIDER={settings.food_data_provider!r} no soportado"
    )
