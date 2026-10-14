"""API pública de users. Otros módulos solo importan desde aquí."""

from app.modules.users.dependencies import CurrentUserDep, get_current_user
from app.modules.users.domain import ActivityLevel, CurrentUser, Profile, Sex, UnitSystem
from app.modules.users.router import router
from app.modules.users.service import get_profile

__all__ = [
    "ActivityLevel",
    "CurrentUser",
    "CurrentUserDep",
    "Profile",
    "Sex",
    "UnitSystem",
    "get_current_user",
    "get_profile",
    "router",
]
