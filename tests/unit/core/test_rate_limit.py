import pytest

from app.core.errors import RateLimitedError
from app.core.rate_limit import KeyedRateLimiter


def test_blocks_after_limit_per_key() -> None:
    limiter = KeyedRateLimiter("2/minute")
    limiter.hit("login", "a@x.com")
    limiter.hit("login", "a@x.com")
    with pytest.raises(RateLimitedError):
        limiter.hit("login", "a@x.com")
    limiter.hit("login", "b@x.com")


def test_multiple_windows_apply() -> None:
    limiter = KeyedRateLimiter("10/minute;1/hour")
    limiter.hit("login", "a@x.com")
    with pytest.raises(RateLimitedError):
        limiter.hit("login", "a@x.com")


def test_disabled_never_blocks() -> None:
    limiter = KeyedRateLimiter("1/minute", enabled=False)
    for _ in range(5):
        limiter.hit("login", "a@x.com")


def test_reset_clears_counters() -> None:
    limiter = KeyedRateLimiter("1/minute")
    limiter.hit("login", "a@x.com")
    limiter.reset()
    limiter.hit("login", "a@x.com")
