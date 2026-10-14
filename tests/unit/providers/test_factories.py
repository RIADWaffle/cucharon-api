import pytest

from app.core.config import Settings
from app.providers.errors import ProviderNotConfiguredError
from app.providers.food_data import get_food_data_provider
from app.providers.llm import get_llm_provider
from app.providers.storage import get_storage_provider
from app.providers.storage.local_fs import LocalFsProvider


def test_storage_factory_builds_local_fs(test_settings: Settings) -> None:
    assert isinstance(get_storage_provider(test_settings), LocalFsProvider)


def test_storage_factory_requires_local_secret(test_settings: Settings) -> None:
    settings = test_settings.model_copy(update={"storage_local_secret": None})
    with pytest.raises(ProviderNotConfiguredError):
        get_storage_provider(settings)


def test_llm_factory_has_no_implementation_yet(test_settings: Settings) -> None:
    with pytest.raises(ProviderNotConfiguredError):
        get_llm_provider(test_settings)


def test_food_data_factory_has_no_implementation_yet(test_settings: Settings) -> None:
    with pytest.raises(ProviderNotConfiguredError):
        get_food_data_provider(test_settings)
