from sqlalchemy import Column, ForeignKey, Table
from sqlalchemy.dialects.postgresql import UUID as PostgreSQL_UUID

from app.core.database import Base


map_tags = Table(
    "map_tags",
    Base.metadata,
    Column(
        "map_id",
        PostgreSQL_UUID(as_uuid=True),
        ForeignKey("maps.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "tag_id",
        PostgreSQL_UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)
