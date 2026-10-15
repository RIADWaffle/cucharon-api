"""API pública de products. Otros módulos solo importan desde aquí."""

from app.modules.products.domain import NutrientBasis, ServingUnit, normalize_gtin
from app.modules.products.router import router
from app.modules.products.service import ProductView, get_product

__all__ = ["NutrientBasis", "ProductView", "ServingUnit", "get_product", "normalize_gtin", "router"]
