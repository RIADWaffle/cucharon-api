from typing import Any, ClassVar, cast

import structlog
from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = structlog.get_logger(__name__)


class DomainError(Exception):
    """Base de las excepciones de dominio. Los services lanzan subclases de esta."""

    code: ClassVar[str] = "domain_error"
    default_message: ClassVar[str] = "Error de dominio"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


class NotFoundError(DomainError):
    code = "not_found"
    default_message = "Recurso no encontrado"


class ConflictError(DomainError):
    code = "conflict"
    default_message = "Conflicto con el estado actual"


class UnauthorizedError(DomainError):
    code = "unauthorized"
    default_message = "No autenticado"


class ForbiddenError(DomainError):
    code = "forbidden"
    default_message = "Sin permiso"


class DomainValidationError(DomainError):
    code = "validation_error"
    default_message = "Datos inválidos"


class RateLimitedError(DomainError):
    code = "rate_limited"
    default_message = "Demasiadas solicitudes, intenta más tarde"


_STATUS_BY_TYPE: list[tuple[type[DomainError], int]] = [
    (NotFoundError, status.HTTP_404_NOT_FOUND),
    (ConflictError, status.HTTP_409_CONFLICT),
    (UnauthorizedError, status.HTTP_401_UNAUTHORIZED),
    (ForbiddenError, status.HTTP_403_FORBIDDEN),
    (DomainValidationError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (RateLimitedError, status.HTTP_429_TOO_MANY_REQUESTS),
]


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: Any | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, details=details))
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(body, exclude_none=True),
        headers=headers,
    )


def status_for(exc: DomainError) -> int:
    for exc_type, status_code in _STATUS_BY_TYPE:
        if isinstance(exc, exc_type):
            return status_code
    return status.HTTP_400_BAD_REQUEST


async def _domain_error_handler(_: Request, exc: Exception) -> JSONResponse:
    exc = cast(DomainError, exc)
    status_code = status_for(exc)
    headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else None
    return error_response(status_code, exc.code, exc.message, headers=headers)


async def _validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    exc = cast(RequestValidationError, exc)
    return error_response(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "validation_error",
        "Datos inválidos",
        # Sin `input` ni `ctx`: no se refleja lo enviado (p. ej. passwords) y `ctx`
        # puede traer excepciones que no son serializables.
        details=[
            {"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]}
            for err in exc.errors()
        ],
    )


async def _http_error_handler(_: Request, exc: Exception) -> JSONResponse:
    exc = cast(StarletteHTTPException, exc)
    return error_response(
        exc.status_code,
        "http_error",
        str(exc.detail),
        headers=dict(exc.headers) if exc.headers else None,
    )


async def _rate_limit_handler(_: Request, exc: Exception) -> JSONResponse:
    exc = cast(RateLimitExceeded, exc)
    return error_response(
        status.HTTP_429_TOO_MANY_REQUESTS, RateLimitedError.code, RateLimitedError.default_message
    )


async def _unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_error", exc_info=exc)
    return error_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error", "Error interno del servidor"
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, _domain_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
