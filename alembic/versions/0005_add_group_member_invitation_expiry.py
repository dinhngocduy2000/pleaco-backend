"""Track the active invitation on pending group memberships.

Revision ID: 0005_member_invite_expiry
Revises: 0004_group_member_status
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0005_member_invite_expiry"
down_revision: Union[str, None] = "0004_group_member_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "group_members",
        sa.Column("invitation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "group_members",
        sa.Column("invitation_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_group_members_pending_invitation_expiry",
        "group_members",
        ["invitation_expires_at"],
        postgresql_where=sa.text("invitation_status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_group_members_pending_invitation_expiry", table_name="group_members")
    op.drop_column("group_members", "invitation_expires_at")
    op.drop_column("group_members", "invitation_id")
