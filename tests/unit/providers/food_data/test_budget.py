import pytest

from app.providers.errors import ProviderUnavailableError
from app.providers.food_data.budget import OutboundBudget


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def test_budget_is_per_kind_and_reports_retry_after() -> None:
    budget = OutboundBudget("off", {"product": "2/minute", "search": "1/minute"})
    budget.acquire("product")
    budget.acquire("product")
    budget.acquire("search")
    with pytest.raises(ProviderUnavailableError) as exc_info:
        budget.acquire("product")
    assert exc_info.value.provider == "off"
    assert exc_info.value.retry_after is not None
    assert exc_info.value.retry_after > 0


def test_multiple_windows_and_reset() -> None:
    budget = OutboundBudget("usda", {"search": "1/second;5/minute"})
    budget.acquire("search")
    for _ in range(3):
        with pytest.raises(ProviderUnavailableError):
            budget.acquire("search")
    budget.reset()
    budget.acquire("search")


def test_pause_blocks_until_it_expires() -> None:
    clock = Clock()
    budget = OutboundBudget("off", {"search": "100/minute"}, clock=clock)
    budget.pause("search", 60)
    with pytest.raises(ProviderUnavailableError) as exc_info:
        budget.acquire("search")
    assert exc_info.value.retry_after == 60
    clock.now += 61
    budget.acquire("search")
