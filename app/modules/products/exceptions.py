from app.core.errors import (
    ConflictError,
    DomainValidationError,
    NotFoundError,
    ServiceUnavailableError,
)


class InvalidBarcodeError(DomainValidationError):
    code = "invalid_barcode"
    default_message = "Código de barras inválido (UPC-A, EAN-8, EAN-13 o GTIN-14)"


class ProductNotFoundError(NotFoundError):
    # En el escaneo: el cliente ofrece crear un producto custom.
    code = "product_not_found"
    default_message = "Producto no encontrado"


class CustomProductNotFoundError(NotFoundError):
    code = "custom_product_not_found"
    default_message = "Producto propio no encontrado"


class OverrideExistsError(ConflictError):
    code = "override_exists"
    default_message = "Ya tienes una versión propia de este producto"


class BarcodeTakenError(ConflictError):
    code = "barcode_taken"
    default_message = "Ya tienes un producto con ese código de barras"


class CustomProductIdTakenError(ConflictError):
    code = "id_taken"
    default_message = "Ya existe un producto con ese id"


class FoodDataUnavailableError(ServiceUnavailableError):
    code = "food_data_unavailable"
    default_message = "La base de alimentos externa no responde; intenta más tarde"
