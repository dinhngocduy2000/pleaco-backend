"""Add many-to-many tags for maps and robots.

Revision ID: 0007_add_map_robot_tags
Revises: 0006_create_maps_robots
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0007_add_map_robot_tags"
down_revision: Union[str, None] = "0006_create_maps_robots"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "map_tags",
        sa.Column(
            "map_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("maps.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "tag_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tags.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
    )
    op.create_index("ix_map_tags_tag_id", "map_tags", ["tag_id"])

    op.create_table(
        "robot_tags",
        sa.Column(
            "robot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("robots.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "tag_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tags.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
    )
    op.create_index("ix_robot_tags_tag_id", "robot_tags", ["tag_id"])


def downgrade() -> None:
    op.drop_index("ix_robot_tags_tag_id", table_name="robot_tags")
    op.drop_table("robot_tags")
    op.drop_index("ix_map_tags_tag_id", table_name="map_tags")
    op.drop_table("map_tags")
