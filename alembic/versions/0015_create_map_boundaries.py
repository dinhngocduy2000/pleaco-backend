"""Create map boundary storage using PostGIS.

Revision ID: 0015_create_map_boundaries
Revises: 0014_map_dimensions_numeric
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql


revision: str = "0015_create_map_boundaries"
down_revision: Union[str, None] = "0014_map_dimensions_numeric"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    source = postgresql.ENUM(
        "DIMENSIONS",
        "CUSTOM",
        "TEACH_MODE",
        name="map_boundary_source",
        create_type=False,
    )
    source.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "map_boundaries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "map_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("maps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", source, nullable=False, server_default=sa.text("'DIMENSIONS'")),
        sa.Column(
            "geometry",
            Geometry(geometry_type="POLYGON", srid=0, dimension=2, spatial_index=False),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("map_id", name="uq_map_boundaries_map_id"),
        sa.CheckConstraint("ST_IsValid(geometry)", name="ck_map_boundaries_geometry_valid"),
        sa.CheckConstraint(
            "NOT ST_IsEmpty(geometry)", name="ck_map_boundaries_geometry_not_empty"
        ),
        sa.CheckConstraint("ST_SRID(geometry) = 0", name="ck_map_boundaries_geometry_srid"),
    )


def downgrade() -> None:
    op.drop_table("map_boundaries")
    postgresql.ENUM(name="map_boundary_source").drop(op.get_bind(), checkfirst=True)
    # PostGIS is shared infrastructure and may be used by other tables.
