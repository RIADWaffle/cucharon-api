from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String, Text, func, true
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin, text_enum
from app.modules.users.domain import ActivityLevel, Sex, UnitSystem


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("email = lower(email)", name="email_lowercase"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # Todos los tokens nacidos de un mismo login comparten family_id. Ver ADR 0002.
    family_id: Mapped[UUID] = mapped_column(index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    replaced_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("refresh_tokens.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class UserProfile(TimestampMixin, Base):
    __tablename__ = "user_profiles"
    __table_args__ = (CheckConstraint("height_cm > 0 AND height_cm < 300", name="height_cm_range"),)

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    display_name: Mapped[str | None] = mapped_column(String(100))
    birth_date: Mapped[date | None]
    sex: Mapped[Sex | None] = mapped_column(text_enum(Sex, "sex"))
    height_cm: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    activity_level: Mapped[ActivityLevel | None] = mapped_column(
        text_enum(ActivityLevel, "activity_level")
    )
    timezone: Mapped[str] = mapped_column(String(64))
    unit_system: Mapped[UnitSystem] = mapped_column(
        text_enum(UnitSystem, "unit_system"),
        default=UnitSystem.METRIC,
        server_default=UnitSystem.METRIC.value,
    )
