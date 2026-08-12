from datetime import datetime
from typing import TYPE_CHECKING
from sqlalchemy import DateTime, ForeignKey, String, func
from app.core.database import Base
from sqlalchemy.dialects.postgresql import UUID as PostgreSQL_UUID
from uuid import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.tag import Tag
    from app.models.event import Event


class EventTag(Base):
    __tablename__ = "event_tags"

    event_id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True),
        ForeignKey("events.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    tag_id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    tag: Mapped["Tag"] = relationship("Tag", back_populates="events")  # type: ignore
    event: Mapped["Event"] = relationship("Event", back_populates="tags")  # type: ignore
