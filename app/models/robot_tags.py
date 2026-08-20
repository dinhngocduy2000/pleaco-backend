from sqlalchemy import Column, ForeignKey, Table
from sqlalchemy.dialects.postgresql import UUID as PostgreSQL_UUID

from app.core.database import Base


robot_tags = Table(
    "robot_tags",
    Base.metadata,
    Column(
        "robot_id",
        PostgreSQL_UUID(as_uuid=True),
        ForeignKey("robots.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "tag_id",
        PostgreSQL_UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)
