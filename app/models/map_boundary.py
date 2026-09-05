from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from geoalchemy2 import Geometry, WKBElement
from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PostgreSQL_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.common.enum.map import MapBoundarySource
from app.core.database import Base

if TYPE_CHECKING:
    from app.models.map import Map


class LocalMapGeometry(Geometry):
    """Decode SRID-0 WKB without mistaking the ring count for an SRID.

    PostGIS omits the SRID flag for local coordinates. GeoAlchemy2 0.20.0
    forces extended WKB decoding, so detect the flag from the actual payload.
    """

    cache_ok = True

    def result_processor(self, dialect, coltype):
        def process(value):
            if value is None:
                return None
            # Normalize to EWKB so re-binding does not require Shapely's WKT conversion.
            return WKBElement(value, srid=self.srid, extended=None).as_ewkb()

        return process


class MapBoundary(Base):
    """One current permitted travel polygon in the map's local X/Y coordinates."""

    __tablename__ = "map_boundaries"
    __table_args__ = (
        UniqueConstraint("map_id", name="uq_map_boundaries_map_id"),
        CheckConstraint("ST_IsValid(geometry)", name="ck_map_boundaries_geometry_valid"),
        CheckConstraint(
            "NOT ST_IsEmpty(geometry)", name="ck_map_boundaries_geometry_not_empty"
        ),
        CheckConstraint("ST_SRID(geometry) = 0", name="ck_map_boundaries_geometry_srid"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True), primary_key=True, default=uuid4, nullable=False
    )
    map_id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True),
        ForeignKey("maps.id", ondelete="CASCADE"),
        nullable=False,
    )
    source: Mapped[MapBoundarySource] = mapped_column(
        Enum(
            MapBoundarySource,
            name="map_boundary_source",
            values_callable=lambda sources: [source.value for source in sources],
        ),
        nullable=False,
        default=MapBoundarySource.DIMENSIONS,
        server_default=MapBoundarySource.DIMENSIONS.value,
    )
    geometry: Mapped[WKBElement] = mapped_column(
        LocalMapGeometry(geometry_type="POLYGON", srid=0, dimension=2, spatial_index=False),
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

    map: Mapped["Map"] = relationship("Map", back_populates="boundary")
