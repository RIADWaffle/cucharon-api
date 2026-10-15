from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.core import health
from app.core.config import Settings, get_settings
from app.core.db import create_engine, create_sessionmaker
from app.core.errors import register_error_handlers
from app.core.http import create_http_client
from app.core.logging import configure_logging, log_requests
from app.core.rate_limit import KeyedRateLimiter, configure_rate_limits, limiter
from app.modules import products, users
from app.providers.food_data import build_food_data_provider


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        configure_logging(settings.log_level, json=settings.log_json)
        engine = create_engine(settings)
        app.state.sessionmaker = create_sessionmaker(engine)
        http_client = create_http_client(settings)
        app.state.food_data_provider = build_food_data_provider(settings, http_client)
        try:
            yield
        finally:
            await http_client.aclose()
            await engine.dispose()

    app = FastAPI(title="Cucharón API", version=__version__, lifespan=lifespan)
    app.state.settings = settings

    configure_rate_limits(settings)
    app.state.limiter = limiter
    app.state.login_email_limiter = KeyedRateLimiter(
        settings.rate_limit_login_email, enabled=settings.rate_limit_enabled
    )
    # Por usuario: cada request puede pegarle a una API externa con cuota compartida.
    app.state.products_barcode_limiter = KeyedRateLimiter(
        settings.rate_limit_products_barcode, enabled=settings.rate_limit_enabled
    )
    app.state.products_search_limiter = KeyedRateLimiter(
        settings.rate_limit_products_search, enabled=settings.rate_limit_enabled
    )

    register_error_handlers(app)
    app.middleware("http")(log_requests)
    app.include_router(health.router)
    app.include_router(users.router)
    app.include_router(products.router)
    return app


app = create_app()
