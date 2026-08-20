"""Create maps and robots tables.

Revision ID: 0006_create_maps_robots
Revises: 0005_member_invite_expiry
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0006_create_maps_robots"
down_revision: Union[str, None] = "0005_member_invite_expiry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    robot_model = postgresql.ENUM(
        "STANDARD", "LITE", "PRO", name="robot_model", create_type=False
    )
    robot_connection_status = postgresql.ENUM(
        "ONLINE", "STALE", "OFFLINE", name="robot_connection_status", create_type=False
    )
    robot_operational_status = postgresql.ENUM(
        "IDLE", "EXECUTING", "CHARGING", name="robot_operational_status", create_type=False
    )

    robot_model.create(op.get_bind(), checkfirst=True)
    robot_connection_status.create(op.get_bind(), checkfirst=True)
    robot_operational_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "maps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_maps_group_id", "maps", ["group_id"])

    op.create_table(
        "robots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "map_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("maps.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("serial_num", sa.String(), nullable=False),
        sa.Column("model", robot_model, nullable=False),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column(
            "connection_status", robot_connection_status, nullable=False
        ),
        sa.Column(
            "operational_status", robot_operational_status, nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("group_id", "serial_num", name="uq_robots_group_serial_num"),
    )
    op.create_index("ix_robots_group_id", "robots", ["group_id"])
    op.create_index("ix_robots_map_id", "robots", ["map_id"])


def downgrade() -> None:
    op.drop_index("ix_robots_map_id", table_name="robots")
    op.drop_index("ix_robots_group_id", table_name="robots")
    op.drop_table("robots")
    op.drop_index("ix_maps_group_id", table_name="maps")
    op.drop_table("maps")

    postgresql.ENUM(name="robot_operational_status").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="robot_connection_status").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="robot_model").drop(op.get_bind(), checkfirst=True)
