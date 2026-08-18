"""Add invitation status to group memberships.

Revision ID: 0004_group_member_status
Revises: 0003_create_tags_table
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0004_group_member_status"
down_revision: Union[str, None] = "0003_create_tags_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    invitation_status = postgresql.ENUM(
        "pending",
        "accepted",
        "rejected",
        name="group_member_status",
        create_type=False,
    )
    invitation_status.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "group_members",
        sa.Column(
            "invitation_status",
            invitation_status,
            nullable=False,
            server_default=sa.text("'accepted'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("group_members", "invitation_status")
    postgresql.ENUM(name="group_member_status").drop(
        op.get_bind(), checkfirst=True
    )
