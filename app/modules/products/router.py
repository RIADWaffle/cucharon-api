from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, status
from fastapi.responses import JSONResponse

from app.core.config import SettingsDep
from app.core.db import SessionDep
from app.core.errors import ErrorResponse, error_response
from app.modules.products import service
from app.modules.products.dependencies import (
    BarcodeLimiterDep,
    FoodDataProviderDep,
    SearchLimiterDep,
)
from app.modules.products.exceptions import ProductNotFoundError
from app.modules.products.schemas import (
    CustomProductCreate,
    CustomProductUpdate,
    ProductOut,
    SearchResponse,
)
from app.modules.users import CurrentUserDep

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}

router = APIRouter(prefix="/products", tags=["products"], responses=_ERRORS)


def _out(view: service.ProductView) -> ProductOut:
    return ProductOut.model_validate(view, from_attributes=True)


@router.get("/barcode/{code}", response_model=ProductOut)
async def lookup_barcode(
    code: str,
    user: CurrentUserDep,
    session: SessionDep,
    provider: FoodDataProviderDep,
    settings: SettingsDep,
    limiter: BarcodeLimiterDep,
) -> ProductOut | JSONResponse:
    """Override/custom del usuario -> caché -> Open Food Facts. 404 = ofrecer custom."""
    limiter.hit("products_barcode", str(user.id))
    view = await service.lookup_barcode(
        session, provider, user_id=user.id, code=code, settings=settings
    )
    if view is None:
        # Se REGRESA el 404 en vez de lanzarlo: ante una excepción `get_session` hace
        # rollback y se perdería la caché negativa recién escrita.
        not_found = ProductNotFoundError()
        return error_response(status.HTTP_404_NOT_FOUND, not_found.code, not_found.message)
    return _out(view)


@router.get("/search")
async def search(
    q: Annotated[str, Query(min_length=2, max_length=100)],
    user: CurrentUserDep,
    session: SessionDep,
    provider: FoodDataProviderDep,
    limiter: SearchLimiterDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> SearchResponse:
    """Buscar al enviar, no mientras se escribe (cuota de OFF). Ver CLIENT_NOTES."""
    limiter.hit("products_search", str(user.id))
    result = await service.search(session, provider, user_id=user.id, query=q, limit=limit)
    return SearchResponse.model_validate(result, from_attributes=True)


@router.post("/custom", status_code=status.HTTP_201_CREATED)
async def create_custom(
    body: CustomProductCreate, user: CurrentUserDep, session: SessionDep
) -> ProductOut:
    fields = body.model_dump(exclude_unset=True)
    return _out(await service.create_custom(session, user.id, fields))


@router.get("/custom/{product_id}")
async def get_custom(product_id: UUID, user: CurrentUserDep, session: SessionDep) -> ProductOut:
    return _out(await service.get_custom(session, user.id, product_id))


@router.patch("/custom/{product_id}")
async def update_custom(
    product_id: UUID, body: CustomProductUpdate, user: CurrentUserDep, session: SessionDep
) -> ProductOut:
    fields = body.model_dump(exclude_unset=True)
    return _out(await service.update_custom(session, user.id, product_id, fields))


@router.delete("/custom/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_custom(product_id: UUID, user: CurrentUserDep, session: SessionDep) -> None:
    await service.delete_custom(session, user.id, product_id)


@router.get("/{product_id}")
async def get_product(product_id: UUID, user: CurrentUserDep, session: SessionDep) -> ProductOut:
    """Producto de la caché global por id (p. ej. el que referencia una receta)."""
    return _out(await service.get_product(session, product_id))
