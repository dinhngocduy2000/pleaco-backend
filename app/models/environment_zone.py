from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from geoalchemy2 import WKBElement
from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID as PostgreSQL_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.common.enum.environment_zone import EnvironmentZoneType
from app.core.database import Base
from app.models.geometry import LocalMapGeometry

if TYPE_CHECKING:
    from app.models.map import Map


class EnvironmentZone(Base):
    """A typed polygonal section in a map's local X/Y coordinates."""

    __tablename__ = "environment_zones"
    __table_args__ = (
        CheckConstraint(
            "ST_IsValid(geometry)", name="ck_environment_zones_geometry_valid"
        ),
        CheckConstraint(
            "NOT ST_IsEmpty(geometry)",
            name="ck_environment_zones_geometry_not_empty",
        ),
        CheckConstraint(
            "ST_SRID(geometry) = 0", name="ck_environment_zones_geometry_srid"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True), primary_key=True, default=uuid4, nullable=False
    )
    type: Mapped[EnvironmentZoneType] = mapped_column(
        Enum(
            EnvironmentZoneType,
            name="environment_zone_type",
            values_callable=lambda zone_types: [
                zone_type.value for zone_type in zone_types
            ],
        ),
        nullable=False,
    )
    geometry: Mapped[WKBElement] = mapped_column(
        LocalMapGeometry(
            geometry_type="POLYGON", srid=0, dimension=2, spatial_index=False
        ),
        nullable=False,
    )
    map_id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True),
        ForeignKey("maps.id", ondelete="CASCADE"),
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

    map: Mapped["Map"] = relationship("Map", back_populates="environment_zones")
