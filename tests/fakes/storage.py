from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.providers.storage import PresignedDownload, PresignedUpload, StorageProvider
from app.providers.storage.base import validate_key

FAKE_STORAGE_BASE_URL = "https://fake-storage.test"
FAKE_NOW = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class FakeStorageProvider:
    """No sube nada: registra presigns y deletes. URLs deterministas."""

    uploads: list[tuple[str, str, int]] = field(default_factory=list[tuple[str, str, int]])
    deleted: list[str] = field(default_factory=list[str])

    async def presign_upload(self, key: str, content_type: str, max_bytes: int) -> PresignedUpload:
        validate_key(key)
        self.uploads.append((key, content_type, max_bytes))
        return PresignedUpload(
            url=f"{FAKE_STORAGE_BASE_URL}/upload/{key}",
            method="PUT",
            expires_at=FAKE_NOW + timedelta(minutes=15),
            headers={"Content-Type": content_type},
        )

    async def presign_download(self, key: str) -> PresignedDownload:
        validate_key(key)
        return PresignedDownload(
            url=f"{FAKE_STORAGE_BASE_URL}/download/{key}",
            expires_at=FAKE_NOW + timedelta(hours=1),
        )

    async def delete(self, key: str) -> None:
        validate_key(key)
        self.deleted.append(key)


_: type[StorageProvider] = FakeStorageProvider
