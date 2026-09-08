"""Add map boundary map ID index.

Revision ID: 0016_map_boundary_map_id_index
Revises: 0015_create_map_boundaries
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0016_map_boundary_map_id_index"
down_revision: Union[str, None] = "0015_create_map_boundaries"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_map_boundaries_map_id", "map_boundaries", ["map_id"])


def downgrade() -> None:
    op.drop_index("ix_map_boundaries_map_id", table_name="map_boundaries")
