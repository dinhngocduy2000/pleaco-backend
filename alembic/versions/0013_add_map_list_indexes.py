"""Add map list indexes.

Revision ID: 0013_map_list_indexes
Revises: 0012_map_creation_fields
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0013_map_list_indexes"
down_revision: Union[str, None] = "0012_map_creation_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_index("ix_maps_status", "maps", ["status"])
    op.create_index(
        "ix_maps_name_trgm",
        "maps",
        ["name"],
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_maps_name_trgm", table_name="maps")
    op.drop_index("ix_maps_status", table_name="maps")
