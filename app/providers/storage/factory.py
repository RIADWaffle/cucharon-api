from pathlib import Path

from app.core.config import SettingsDep
from app.providers.errors import ProviderNotConfiguredError
from app.providers.storage.base import StorageProvider
from app.providers.storage.local_fs import LocalFsProvider


def get_storage_provider(settings: SettingsDep) -> StorageProvider:
    """Dependency de FastAPI. Elige la implementación según `STORAGE_PROVIDER`."""
    match settings.storage_provider:
        case "local":
            if settings.storage_local_secret is None:
                raise ProviderNotConfiguredError("STORAGE_LOCAL_SECRET es obligatorio")
            return LocalFsProvider(
                root=Path(settings.storage_local_dir),
                base_url=settings.storage_local_base_url,
                secret=settings.storage_local_secret.get_secret_value(),
            )
