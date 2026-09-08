"""Create environment zones.

Revision ID: 0017_create_environment_zones
Revises: 0016_map_boundary_map_id_index
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql


revision: str = "0017_create_environment_zones"
down_revision: Union[str, None] = "0016_map_boundary_map_id_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    zone_type = postgresql.ENUM(
        "NO_GO",
        "OBSTACLE",
        "CLEANING_ZONE",
        name="environment_zone_type",
        create_type=False,
    )
    zone_type.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "environment_zones",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column("type", zone_type, nullable=False),
        sa.Column(
            "geometry",
            Geometry(geometry_type="POLYGON", srid=0, dimension=2, spatial_index=False),
            nullable=False,
        ),
        sa.Column(
            "map_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("maps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "ST_IsValid(geometry)", name="ck_environment_zones_geometry_valid"
        ),
        sa.CheckConstraint(
            "NOT ST_IsEmpty(geometry)",
            name="ck_environment_zones_geometry_not_empty",
        ),
        sa.CheckConstraint(
            "ST_SRID(geometry) = 0", name="ck_environment_zones_geometry_srid"
        ),
    )
    op.create_index("ix_environment_zones_map_id", "environment_zones", ["map_id"])


def downgrade() -> None:
    op.drop_index("ix_environment_zones_map_id", table_name="environment_zones")
    op.drop_table("environment_zones")
    postgresql.ENUM(name="environment_zone_type").drop(op.get_bind(), checkfirst=True)
    # PostGIS is shared infrastructure and must remain installed.
