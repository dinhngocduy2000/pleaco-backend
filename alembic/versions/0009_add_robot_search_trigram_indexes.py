"""Add trigram indexes for robot list search.

Revision ID: 0009_robot_search_trgm
Revises: 0008_robot_model_created_index
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0009_robot_search_trgm"
down_revision: Union[str, None] = "0008_robot_model_created_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_index(
        "ix_robots_name_trgm",
        "robots",
        ["name"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_robots_serial_num_trgm",
        "robots",
        ["serial_num"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"serial_num": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_robots_serial_num_trgm", table_name="robots")
    op.drop_index("ix_robots_name_trgm", table_name="robots")
