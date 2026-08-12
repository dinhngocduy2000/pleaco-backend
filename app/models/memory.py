from datetime import datetime
from typing import List
from uuid import UUID

from sqlalchemy import DateTime, Enum, ForeignKey, String, func
from app.common.enum.memory_visibility import MemoryVisibility
from app.core.database import Base
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID as PostgreSQL_UUID
from uuid import UUID, uuid4


class Memory(Base):
    __tablename__ = "memories"
    id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True), primary_key=True, default=uuid4, nullable=False
    )

    event_id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True),
        ForeignKey("events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    created_by: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    title: Mapped[str] = mapped_column(String(50), nullable=False)
    caption: Mapped[str] = mapped_column(String(255), nullable=True)
    visibility: Mapped[MemoryVisibility] = mapped_column(
        Enum(MemoryVisibility),
        nullable=False,
        default=MemoryVisibility.GROUP,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    comments: Mapped[List["Comment"]] = relationship("Comment", back_populates="memory")  # type: ignore
    event: Mapped["Event"] = relationship("Event")  # type: ignore
    owner: Mapped["User"] = relationship("User")  # type: ignore
