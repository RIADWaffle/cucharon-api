import hashlib
import hmac
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode

import anyio

from app.providers.storage.base import PresignedDownload, PresignedUpload, validate_key


def _utcnow() -> datetime:
    return datetime.now(UTC)


class LocalFsProvider:
    """Storage en disco para desarrollo.

    Firma URLs con HMAC. La ruta que recibe los uploads/descargas y llama a
    `verify_signature` se agrega con el módulo recipes; aquí solo se firma.
    """

    def __init__(
        self,
        root: Path,
        base_url: str,
        secret: str,
        *,
        upload_ttl: timedelta = timedelta(minutes=15),
        download_ttl: timedelta = timedelta(hours=1),
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._root = root
        self._base_url = base_url.rstrip("/")
        self._secret = secret.encode()
        self._upload_ttl = upload_ttl
        self._download_ttl = download_ttl
        self._clock = clock

    async def presign_upload(self, key: str, content_type: str, max_bytes: int) -> PresignedUpload:
        validate_key(key)
        expires_at = self._clock() + self._upload_ttl
        params = {
            "expires": str(int(expires_at.timestamp())),
            "content_type": content_type,
            "max_bytes": str(max_bytes),
        }
        url = self._signed_url("PUT", key, params)
        return PresignedUpload(
            url=url, method="PUT", expires_at=expires_at, headers={"Content-Type": content_type}
        )

    async def presign_download(self, key: str) -> PresignedDownload:
        validate_key(key)
        expires_at = self._clock() + self._download_ttl
        params = {"expires": str(int(expires_at.timestamp()))}
        return PresignedDownload(url=self._signed_url("GET", key, params), expires_at=expires_at)

    async def delete(self, key: str) -> None:
        path = anyio.Path(self.path_for(key))
        await path.unlink(missing_ok=True)

    def path_for(self, key: str) -> Path:
        return self._root / validate_key(key)

    def verify_signature(
        self, method: str, key: str, params: dict[str, str], signature: str
    ) -> bool:
        """Valida firma y expiración. `params` = query string sin `signature`."""
        expected = self._sign(method, key, params)
        if not hmac.compare_digest(expected, signature):
            return False
        try:
            expires = int(params["expires"])
        except (KeyError, ValueError):
            return False
        return self._clock().timestamp() <= expires

    def _signed_url(self, method: str, key: str, params: dict[str, str]) -> str:
        signature = self._sign(method, key, params)
        query = urlencode({**params, "signature": signature})
        return f"{self._base_url}/{quote(key)}?{query}"

    def _sign(self, method: str, key: str, params: dict[str, str]) -> str:
        canonical = "\n".join([method, key, *(f"{k}={params[k]}" for k in sorted(params))])
        return hmac.new(self._secret, canonical.encode(), hashlib.sha256).hexdigest()
