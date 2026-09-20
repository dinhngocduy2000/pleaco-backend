"""Create docking station.

Revision ID: 0018_create_docking_station
Revises: 0017_create_environment_zones
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql


revision: str = "0018_create_docking_station"
down_revision: Union[str, None] = "0017_create_environment_zones"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    heading = postgresql.ENUM(
        "NORTH",
        "SOUTH",
        "EAST",
        "WEST",
        name="docking_station_heading",
        create_type=False,
    )
    heading.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "docking_station",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column(
            "map_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("maps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "robot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("robots.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "geometry",
            Geometry(geometry_type="POLYGON", srid=0, dimension=2, spatial_index=False),
            nullable=False,
        ),
        sa.Column("heading", heading, nullable=False),
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
        sa.UniqueConstraint("robot_id", name="uq_docking_station_robot_id"),
        sa.CheckConstraint(
            "ST_IsValid(geometry)", name="ck_docking_station_geometry_valid"
        ),
        sa.CheckConstraint(
            "NOT ST_IsEmpty(geometry)", name="ck_docking_station_geometry_not_empty"
        ),
        sa.CheckConstraint(
            "ST_SRID(geometry) = 0", name="ck_docking_station_geometry_srid"
        ),
    )
    op.create_index("ix_docking_station_map_id", "docking_station", ["map_id"])


def downgrade() -> None:
    op.drop_index("ix_docking_station_map_id", table_name="docking_station")
    op.drop_table("docking_station")
    postgresql.ENUM(name="docking_station_heading").drop(op.get_bind(), checkfirst=True)
    # PostGIS is shared infrastructure and must remain installed.
