from typing import Annotated

from fastapi import Depends, Request

from app.core.rate_limit import KeyedRateLimiter
from app.providers.food_data import FoodDataProvider, get_food_data_provider

FoodDataProviderDep = Annotated[FoodDataProvider, Depends(get_food_data_provider)]


def get_barcode_limiter(request: Request) -> KeyedRateLimiter:
    return request.app.state.products_barcode_limiter


def get_search_limiter(request: Request) -> KeyedRateLimiter:
    return request.app.state.products_search_limiter


BarcodeLimiterDep = Annotated[KeyedRateLimiter, Depends(get_barcode_limiter)]
SearchLimiterDep = Annotated[KeyedRateLimiter, Depends(get_search_limiter)]
