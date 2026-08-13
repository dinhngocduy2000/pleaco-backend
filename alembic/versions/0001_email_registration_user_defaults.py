"""Create the users table with secure registration defaults.

Revision ID: 0001_email_registration_user_defaults
Revises: None
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_email_registration"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    user_status = sa.Enum(
        "ACTIVE", "INACTIVE", "PENDING", "DELETED", name="userstatus"
    )
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("email", sa.String(length=50), nullable=False, unique=True),
        sa.Column(
            "status",
            user_status,
            nullable=False,
            server_default=sa.text("'INACTIVE'"),
        ),
        sa.Column("password", sa.String(length=255), nullable=True),
        sa.Column("image_url", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("active_group_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_users_status", "users", ["status"])
    op.create_index("ix_users_active_group_id", "users", ["active_group_id"])


def downgrade() -> None:
    op.drop_index("ix_users_active_group_id", table_name="users")
    op.drop_index("ix_users_status", table_name="users")
    op.drop_table("users")
    sa.Enum("ACTIVE", "INACTIVE", "PENDING", "DELETED", name="userstatus").drop(
        op.get_bind(), checkfirst=True
    )
