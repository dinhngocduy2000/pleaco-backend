"""Add group, model, and creation-time index for robot listings.

Revision ID: 0008_robot_group_model_created_index
Revises: 0007_add_map_robot_tags
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_robot_model_created_index"
down_revision: Union[str, None] = "0007_add_map_robot_tags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_robots_group_model_created_at",
        "robots",
        ["group_id", "model", sa.desc("created_at")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_robots_group_model_created_at", table_name="robots")
