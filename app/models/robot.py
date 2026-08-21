from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, UniqueConstraint, desc, func
from sqlalchemy.dialects.postgresql import INET, UUID as PostgreSQL_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.common.enum.robot import (
    RobotConnectionStatus,
    RobotModel,
    RobotOperationalStatus,
)
from app.core.database import Base
from app.models.robot_tags import robot_tags

if TYPE_CHECKING:
    from app.models.group import Group
    from app.models.map import Map
    from app.models.tag import Tag


class Robot(Base):
    __tablename__ = "robots"
    __table_args__ = (
        UniqueConstraint("group_id", "serial_num", name="uq_robots_group_serial_num"),
        Index(
            "ix_robots_group_model_created_at",
            "group_id",
            "model",
            desc("created_at"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True), primary_key=True, default=uuid4, nullable=False
    )
    group_id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True),
        ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    map_id: Mapped[UUID | None] = mapped_column(
        PostgreSQL_UUID(as_uuid=True),
        ForeignKey("maps.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    serial_num: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[RobotModel] = mapped_column(
        Enum(
            RobotModel,
            name="robot_model",
            values_callable=lambda models: [model.value for model in models],
        ),
        nullable=False,
    )
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    connection_status: Mapped[RobotConnectionStatus] = mapped_column(
        Enum(
            RobotConnectionStatus,
            name="robot_connection_status",
            values_callable=lambda statuses: [status.value for status in statuses],
        ),
        nullable=False,
    )
    operational_status: Mapped[RobotOperationalStatus] = mapped_column(
        Enum(
            RobotOperationalStatus,
            name="robot_operational_status",
            values_callable=lambda statuses: [status.value for status in statuses],
        ),
        nullable=False,
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

    group: Mapped["Group"] = relationship("Group", back_populates="robots")
    map: Mapped["Map | None"] = relationship("Map", back_populates="robots")
    tags: Mapped[list["Tag"]] = relationship(
        "Tag", secondary=robot_tags, back_populates="robots"
    )
