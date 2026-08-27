"""Add map creation fields.

Revision ID: 0012_map_creation_fields
Revises: 0011_add_group_ownership_to_tags
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0012_map_creation_fields"
down_revision: Union[str, None] = "0011_add_group_ownership_to_tags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    map_status = postgresql.ENUM(
        "ASSIGNED", "UNASSIGNED", name="map_status", create_type=False
    )
    map_status.create(op.get_bind(), checkfirst=True)

    op.add_column("maps", sa.Column("description", sa.String(), nullable=True))
    op.add_column(
        "maps",
        sa.Column(
            "status",
            map_status,
            nullable=False,
            server_default=sa.text("'UNASSIGNED'"),
        ),
    )
    op.add_column(
        "maps",
        sa.Column(
            "dimension_x", sa.String(), nullable=False, server_default=sa.text("'0'")
        ),
    )
    op.add_column(
        "maps",
        sa.Column(
            "dimension_y", sa.String(), nullable=False, server_default=sa.text("'0'")
        ),
    )
    op.alter_column("maps", "dimension_x", server_default=None)
    op.alter_column("maps", "dimension_y", server_default=None)
    op.create_unique_constraint("uq_maps_group_name", "maps", ["group_id", "name"])


def downgrade() -> None:
    op.drop_constraint("uq_maps_group_name", "maps", type_="unique")
    op.drop_column("maps", "dimension_y")
    op.drop_column("maps", "dimension_x")
    op.drop_column("maps", "status")
    op.drop_column("maps", "description")
    postgresql.ENUM(name="map_status").drop(op.get_bind(), checkfirst=True)
