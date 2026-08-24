"""Add durable robot connectivity receive time.

Revision ID: 0010_robot_last_seen
Revises: 0009_robot_search_indexes
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0010_robot_last_seen"
down_revision: Union[str, None] = "0009_robot_search_trgm"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("robots", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("robots", sa.Column("last_sequence_number", sa.Integer(), nullable=True))
    op.add_column("robots", sa.Column("last_message_id", sa.UUID(), nullable=True))


def downgrade() -> None:
    op.drop_column("robots", "last_message_id")
    op.drop_column("robots", "last_sequence_number")
    op.drop_column("robots", "last_seen_at")
