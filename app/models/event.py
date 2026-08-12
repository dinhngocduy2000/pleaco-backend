from datetime import datetime
from typing import List, Optional, TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, func, inspect
from app.common.enum.event_priority import EventCategory, EventPriority
from app.common.schemas.events import EventDetailInfo, EventListInfo
from app.core.database import Base
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID as PostgreSQL_UUID

if TYPE_CHECKING:
    from app.models.event_tag import EventTag
    from app.models.user import User


class Event(Base):
    __tablename__ = "events"

    id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        nullable=False,
        index=True,
    )

    group_id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True),
        ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    destination: Mapped[str] = mapped_column(String(255), nullable=False)
    cost: Mapped[str] = mapped_column(String(255), nullable=False)
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    owner_id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    end_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    priority: Mapped[EventPriority] = mapped_column(
        Enum(EventPriority), nullable=False, default=EventPriority.MEDIUM, index=True
    )
    category: Mapped[EventCategory] = mapped_column(
        Enum(EventCategory), nullable=False, default=EventCategory.OTHER, index=True
    )
    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        server_default=func.now(),
        onupdate=func.now(),
    )
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, server_default=func.now()
    )

    tags: Mapped[List["EventTag"]] = relationship("EventTag", back_populates="event")
    owner: Mapped["User"] = relationship("User")  # type: ignore
    __table_args__ = (
        Index("ix_events_group_start_time", "group_id", "start_time"),
        Index("ix_events_group_priority", "group_id", "priority"),
    )

    def viewList(self) -> EventListInfo:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    def viewInfo(self) -> EventDetailInfo:
        raise NotImplementedError("Pleaco-specific implementation is pending.")
