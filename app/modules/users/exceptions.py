from app.core.errors import (
    ConflictError,
    DomainValidationError,
    NotFoundError,
    UnauthorizedError,
)


class EmailAlreadyRegisteredError(ConflictError):
    code = "email_taken"
    default_message = "Ese email ya está registrado"


class InvalidCredentialsError(UnauthorizedError):
    # Genérico a propósito: no revela si el email existe.
    code = "invalid_credentials"
    default_message = "Credenciales inválidas"


class InvalidRefreshTokenError(UnauthorizedError):
    code = "invalid_refresh_token"
    default_message = "Refresh token inválido; inicia sesión de nuevo"


class RefreshTokenReusedError(InvalidRefreshTokenError):
    # Mismo code/mensaje que InvalidRefreshTokenError hacia el cliente. Se lanza
    # después de revocar (y commitear) la familia. Ver ADR 0002.
    pass


class InactiveUserError(UnauthorizedError):
    code = "invalid_token"
    default_message = "Token inválido o expirado"


class ProfileNotFoundError(NotFoundError):
    code = "profile_not_found"
    default_message = "Perfil no encontrado"


class InvalidBirthDateError(DomainValidationError):
    code = "invalid_birth_date"
    default_message = "Fecha de nacimiento inválida"
