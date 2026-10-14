"""Importa los `models` de todos los módulos para que Alembic vea el metadata completo.

Solo lo usan Alembic y los tests. Ningún módulo debe importar este archivo.
"""

from app.modules.users import models as users_models  # noqa: F401  # pyright: ignore[reportUnusedImport]
