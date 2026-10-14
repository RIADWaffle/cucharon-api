from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core import health
from app.core.config import Settings, get_settings
from app.core.db import create_engine, create_sessionmaker
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging, log_requests
from app.core.rate_limit import KeyedRateLimiter, configure_rate_limits, limiter
from app.modules import users


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        configure_logging(settings.log_level, json=settings.log_json)
        engine = create_engine(settings)
        app.state.sessionmaker = create_sessionmaker(engine)
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(title="Cucharón API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings

    configure_rate_limits(settings)
    app.state.limiter = limiter
    app.state.login_email_limiter = KeyedRateLimiter(
        settings.rate_limit_login_email, enabled=settings.rate_limit_enabled
    )

    register_error_handlers(app)
    app.middleware("http")(log_requests)
    app.include_router(health.router)
    app.include_router(users.router)
    return app


app = create_app()
