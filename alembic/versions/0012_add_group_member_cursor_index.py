"""Add the group-member cursor pagination index.

Revision ID: 0012_group_member_cursor_index
Revises: 0011_add_group_ownership_to_tags
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_group_member_cursor_index"
down_revision: Union[str, None] = "0011_add_group_ownership_to_tags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_group_members_group_created_at_member_id",
        "group_members",
        ["group_id", sa.desc("created_at"), sa.desc("member_id")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_group_members_group_created_at_member_id",
        table_name="group_members",
    )
