import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.providers.errors import ProviderNotConfiguredError, ProviderUnavailableError
from app.providers.food_data import build_food_data_provider
from app.providers.food_data.chained import ChainedFoodDataProvider
from app.providers.food_data.off import OpenFoodFactsProvider
from app.providers.food_data.usda import UsdaProvider
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


async def test_food_data_factory_without_providers_never_claims_not_found(
    test_settings: Settings,
) -> None:
    async with httpx.AsyncClient() as client:
        provider = build_food_data_provider(test_settings, client)
        with pytest.raises(ProviderUnavailableError):
            await provider.get_by_barcode("03017620422003")
        assert (await provider.search("avena")).items == []


async def test_food_data_factory_builds_chain_in_order(test_settings: Settings) -> None:
    settings = test_settings.model_copy(
        update={
            "food_data_providers": ["usda", "off"],
            "food_data_contact_email": "dev@example.com",
            "usda_api_key": SecretStr("k"),
        }
    )
    async with httpx.AsyncClient() as client:
        provider = build_food_data_provider(settings, client)
    assert isinstance(provider, ChainedFoodDataProvider)
    assert [type(p) for _, p in provider._providers] == [UsdaProvider, OpenFoodFactsProvider]


async def test_food_data_factory_skips_usda_without_key(test_settings: Settings) -> None:
    settings = test_settings.model_copy(
        update={"food_data_providers": ["off", "usda"], "food_data_contact_email": "a@b.c"}
    )
    async with httpx.AsyncClient() as client:
        provider = build_food_data_provider(settings, client)
    assert isinstance(provider, ChainedFoodDataProvider)
    assert [source for source, _ in provider._providers] == ["off"]
