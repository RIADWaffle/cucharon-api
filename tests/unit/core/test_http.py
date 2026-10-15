from app import __version__
from app.core.config import Settings
from app.core.http import create_http_client


async def test_http_client_identifies_the_app(test_settings: Settings) -> None:
    settings = test_settings.model_copy(update={"food_data_contact_email": "dev@example.com"})
    async with create_http_client(settings) as client:
        assert client.headers["User-Agent"] == f"Cucharon/{__version__} (dev@example.com)"
        assert client.timeout.connect == 3.0
        assert client.timeout.read == 5.0
        assert not client.follow_redirects
