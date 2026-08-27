from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Enum, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PostgreSQL_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.common.enum.map import MapStatus
from app.models.map_tags import map_tags

if TYPE_CHECKING:
    from app.models.group import Group
    from app.models.robot import Robot
    from app.models.tag import Tag


class Map(Base):
    __tablename__ = "maps"
    __table_args__ = (
        UniqueConstraint("group_id", "name", name="uq_maps_group_name"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True), primary_key=True, default=uuid4, nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[MapStatus] = mapped_column(
        Enum(
            MapStatus,
            name="map_status",
            values_callable=lambda statuses: [status.value for status in statuses],
        ),
        nullable=False,
        default=MapStatus.UNASSIGNED,
        server_default=MapStatus.UNASSIGNED.value,
    )
    dimension_x: Mapped[str] = mapped_column(String, nullable=False)
    dimension_y: Mapped[str] = mapped_column(String, nullable=False)
    group_id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True),
        ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
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

    group: Mapped["Group"] = relationship("Group", back_populates="maps")
    robots: Mapped[list["Robot"]] = relationship("Robot", back_populates="map")
    tags: Mapped[list["Tag"]] = relationship(
        "Tag", secondary=map_tags, back_populates="maps"
    )
