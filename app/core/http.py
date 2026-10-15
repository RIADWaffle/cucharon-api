import httpx

from app import __version__
from app.core.config import Settings


def create_http_client(settings: Settings) -> httpx.AsyncClient:
    """Cliente HTTP saliente compartido (uno por proceso, creado en el lifespan).

    Timeouts explícitos y un solo reintento, solo para errores de CONEXIÓN (es lo que
    hace `retries` del transport); nada de reintentos de lectura ni de 5xx. El tope
    total por llamada lo pone cada provider con `FOOD_DATA_TIMEOUT_SECONDS`.
    """
    contact = settings.food_data_contact_email or "contacto no configurado"
    return httpx.AsyncClient(
        # OFF exige "AppName/Version (ContactEmail)".
        headers={"User-Agent": f"Cucharon/{__version__} ({contact})"},
        timeout=httpx.Timeout(5.0, connect=3.0, pool=2.0),
        transport=httpx.AsyncHTTPTransport(
            retries=1,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        ),
        follow_redirects=False,
    )
