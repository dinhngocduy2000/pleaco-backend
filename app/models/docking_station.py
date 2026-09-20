from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from geoalchemy2 import WKBElement
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQL_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.common.enum.docking_station import DockingStationHeading
from app.core.database import Base
from app.models.geometry import LocalMapGeometry

if TYPE_CHECKING:
    from app.models.map import Map
    from app.models.robot import Robot


class DockingStation(Base):
    """A robot's docking polygon and heading in a map's local X/Y coordinates."""

    __tablename__ = "docking_station"
    __table_args__ = (
        UniqueConstraint("robot_id", name="uq_docking_station_robot_id"),
        CheckConstraint(
            "ST_IsValid(geometry)", name="ck_docking_station_geometry_valid"
        ),
        CheckConstraint(
            "NOT ST_IsEmpty(geometry)", name="ck_docking_station_geometry_not_empty"
        ),
        CheckConstraint(
            "ST_SRID(geometry) = 0", name="ck_docking_station_geometry_srid"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True), primary_key=True, default=uuid4, nullable=False
    )
    map_id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True),
        ForeignKey("maps.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    robot_id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True),
        ForeignKey("robots.id", ondelete="CASCADE"),
        nullable=False,
    )
    geometry: Mapped[WKBElement] = mapped_column(
        LocalMapGeometry(
            geometry_type="POLYGON", srid=0, dimension=2, spatial_index=False
        ),
        nullable=False,
    )
    heading: Mapped[DockingStationHeading] = mapped_column(
        Enum(
            DockingStationHeading,
            name="docking_station_heading",
            values_callable=lambda headings: [heading.value for heading in headings],
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

    map: Mapped["Map"] = relationship("Map", back_populates="docking_stations")
    robot: Mapped["Robot"] = relationship("Robot", back_populates="docking_station")
