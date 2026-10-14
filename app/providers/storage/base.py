from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol


@dataclass(frozen=True)
class PresignedUpload:
    url: str
    method: Literal["PUT"]
    expires_at: datetime
    # Headers que el cliente DEBE mandar tal cual en el upload.
    headers: Mapping[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True)
class PresignedDownload:
    url: str
    expires_at: datetime


class InvalidStorageKeyError(ValueError):
    """La key tiene segmentos peligrosos (`..`, absoluta, backslashes, vacía)."""


class StorageProvider(Protocol):
    async def presign_upload(
        self, key: str, content_type: str, max_bytes: int
    ) -> PresignedUpload: ...

    async def presign_download(self, key: str) -> PresignedDownload: ...

    async def delete(self, key: str) -> None: ...


def validate_key(key: str) -> str:
    """Keys relativas tipo `users/<id>/recipes/<id>.jpg`, sin traversal."""
    if not key or key.startswith("/") or "\\" in key or "\x00" in key:
        raise InvalidStorageKeyError(key)
    if any(part in ("", ".", "..") for part in key.split("/")):
        raise InvalidStorageKeyError(key)
    return key
