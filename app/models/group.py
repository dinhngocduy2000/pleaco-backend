from datetime import datetime
from typing import List, TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, String, DateTime, func
from app.core.database import Base
from sqlalchemy.dialects.postgresql import UUID as PostgreSQL_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.map import Map
    from app.models.robot import Robot
    from app.models.user import User


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True), primary_key=True, default=uuid4, nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    description: Mapped[str] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    owner_id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    owner: Mapped["User"] = relationship(
        "User", back_populates="owned_groups", foreign_keys=[owner_id]
    )

    members: Mapped[List["GroupMembers"]] = relationship(  # type: ignore
        "GroupMembers", back_populates="group"
    )
    maps: Mapped[List["Map"]] = relationship("Map", back_populates="group")
    robots: Mapped[List["Robot"]] = relationship("Robot", back_populates="group")
