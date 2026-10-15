import httpx
import pytest
from structlog.testing import capture_logs

from app.providers.errors import ProviderUnavailableError
from app.providers.food_data._http import request_json
from app.providers.food_data.budget import OutboundBudget


async def test_non_json_client_error_is_logged_without_api_key() -> None:
    nginx = httpx.Response(400, text="<html><center>nginx</center></html>")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: nginx)) as client:
        with capture_logs() as logs, pytest.raises(ProviderUnavailableError):
            await request_json(
                client,
                provider="usda",
                kind="search",
                budget=OutboundBudget("usda", {"search": "10/minute"}),
                url="https://usda.test/fdc/v1/foods/search",
                params={"query": "rice", "api_key": "super-secreta"},
                max_seconds=2.0,
            )

    [entry] = [log for log in logs if log["event"] == "provider_non_json_error"]
    assert entry["log_level"] == "error"
    assert entry["status"] == 400
    assert entry["provider"] == "usda"
    assert entry["url"] == "https://usda.test/fdc/v1/foods/search?query=rice"
    assert "super-secreta" not in str(logs)
