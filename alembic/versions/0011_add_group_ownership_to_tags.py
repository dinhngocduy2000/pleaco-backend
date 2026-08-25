"""Add group ownership to tags.

Revision ID: 0011_add_group_ownership_to_tags
Revises: 0010_robot_last_seen
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0011_add_group_ownership_to_tags"
down_revision: Union[str, None] = "0010_robot_last_seen"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tags",
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
    )
    op.create_foreign_key(
        "fk_tags_group_id_groups",
        "tags",
        "groups",
        ["group_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_tags_group_id", "tags", ["group_id"])
    op.drop_index("ix_tags_name", table_name="tags")
    op.create_unique_constraint("uq_tags_group_name", "tags", ["group_id", "name"])


def downgrade() -> None:
    op.drop_constraint("uq_tags_group_name", "tags", type_="unique")
    op.create_index("ix_tags_name", "tags", ["name"], unique=True)
    op.drop_index("ix_tags_group_id", table_name="tags")
    op.drop_constraint("fk_tags_group_id_groups", "tags", type_="foreignkey")
    op.drop_column("tags", "group_id")
