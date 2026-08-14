from datetime import datetime
from typing import List, Optional
from uuid import UUID, uuid4

from sqlalchemy import Enum, String, DateTime, func
from sqlalchemy.dialects.postgresql import UUID as PostgreSQL_UUID
from app.common.enum.user_status import UserStatus
from app.common.schemas.group import GroupInfo
from app.common.schemas.common import HashMapResponse
from app.common.schemas.user import UserInfo
from app.core.database import Base
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    relationship,
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True), primary_key=True, default=uuid4, nullable=False
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    email: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus), nullable=False, default=UserStatus.INACTIVE, index=True
    )
    password: Mapped[str] = mapped_column(String(255), nullable=True)
    image_url: Mapped[str] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    active_group_id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    group_members: Mapped[List["GroupMembers"]] = relationship(  # type: ignore
        "GroupMembers", back_populates="user"
    )

    owned_groups: Mapped[List["Group"]] = relationship(  # type: ignore
        "Group", back_populates="owner"
    )
