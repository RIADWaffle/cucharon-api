from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from app.providers.storage import InvalidStorageKeyError
from app.providers.storage.local_fs import LocalFsProvider

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _provider(tmp_path: Path, clock: Clock) -> LocalFsProvider:
    return LocalFsProvider(tmp_path, "http://localhost:8000/_dev/storage/", "secret", clock=clock)


def _split(url: str) -> tuple[str, dict[str, str], str]:
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query))
    signature = params.pop("signature")
    key = parts.path.removeprefix("/_dev/storage/")
    return key, params, signature


async def test_presign_upload_is_verifiable(tmp_path: Path) -> None:
    clock = Clock(NOW)
    provider = _provider(tmp_path, clock)
    upload = await provider.presign_upload("users/u1/a.jpg", "image/jpeg", 1000)

    assert upload.method == "PUT"
    assert upload.headers == {"Content-Type": "image/jpeg"}
    assert upload.expires_at == NOW + timedelta(minutes=15)
    assert upload.url.startswith("http://localhost:8000/_dev/storage/users/u1/a.jpg?")
    key, params, signature = _split(upload.url)
    assert params["max_bytes"] == "1000"
    assert provider.verify_signature("PUT", key, params, signature)


async def test_signature_rejects_tampering(tmp_path: Path) -> None:
    provider = _provider(tmp_path, Clock(NOW))
    key, params, signature = _split(
        (await provider.presign_upload("users/u1/a.jpg", "image/jpeg", 1000)).url
    )
    assert not provider.verify_signature("PUT", key, {**params, "max_bytes": "999999"}, signature)
    assert not provider.verify_signature("PUT", "users/u2/a.jpg", params, signature)
    assert not provider.verify_signature("GET", key, params, signature)


async def test_signature_expires(tmp_path: Path) -> None:
    clock = Clock(NOW)
    provider = _provider(tmp_path, clock)
    key, params, signature = _split((await provider.presign_download("users/u1/a.jpg")).url)
    assert provider.verify_signature("GET", key, params, signature)
    clock.now = NOW + timedelta(hours=1, seconds=1)
    assert not provider.verify_signature("GET", key, params, signature)


def test_signature_without_expires_is_rejected(tmp_path: Path) -> None:
    provider = _provider(tmp_path, Clock(NOW))
    signature = provider._sign("GET", "a.jpg", {})
    assert not provider.verify_signature("GET", "a.jpg", {}, signature)


async def test_delete_removes_file_and_is_idempotent(tmp_path: Path) -> None:
    provider = _provider(tmp_path, Clock(NOW))
    path = provider.path_for("users/u1/a.jpg")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x")
    await provider.delete("users/u1/a.jpg")
    assert not path.exists()
    await provider.delete("users/u1/a.jpg")


@pytest.mark.parametrize("key", ["", "/abs.jpg", "../x.jpg", "a/../b.jpg", "a//b.jpg", "a\\b.jpg"])
async def test_rejects_unsafe_keys(tmp_path: Path, key: str) -> None:
    provider = _provider(tmp_path, Clock(NOW))
    with pytest.raises(InvalidStorageKeyError):
        await provider.presign_upload(key, "image/jpeg", 10)
