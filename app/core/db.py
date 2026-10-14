import enum
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any, ClassVar

from fastapi import Depends, Request
from sqlalchemy import DateTime, MetaData, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.config import Settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {datetime: DateTime(timezone=True)}


class TimestampMixin:
    """`created_at` / `updated_at` puestos por el servidor (Postgres)."""

    # eager_defaults: trae los valores del servidor con RETURNING tras INSERT/UPDATE;
    # sin esto, leerlos después del flush dispararía un lazy load (truena en async).
    __mapper_args__: ClassVar[dict[str, Any]] = {"eager_defaults": True}

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


def create_engine(settings: Settings) -> AsyncEngine:
    url, connect_args = settings.sqlalchemy_database_url
    return create_async_engine(
        url,
        connect_args=connect_args,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
    )


def create_sessionmaker(
    bind: AsyncEngine | AsyncConnection, **kwargs: Any
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind, expire_on_commit=False, **kwargs)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Una transacción por request: commit al final, rollback si hay excepción.

    Se usa con `scope="function"` (ver `SessionDep`) para que el commit ocurra
    ANTES de mandar la respuesta; con el scope default de FastAPI el cierre corre
    después de enviarla y un fallo de commit quedaría oculto tras un 2xx.
    """
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sessionmaker() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
        else:
            await session.commit()


SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]


def text_enum[E: enum.Enum](enum_cls: type[E], name: str) -> SAEnum:
    """Enum guardado como VARCHAR + CHECK (no enum nativo de Postgres). Ver ADR 0001."""
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        validate_strings=True,
        values_callable=_enum_values,
    )


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [str(member.value) for member in enum_cls]
