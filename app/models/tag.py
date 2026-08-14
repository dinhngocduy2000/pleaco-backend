from datetime import datetime
from typing import List
from sqlalchemy import DateTime, String, func
from app.common.schemas.tags import TagInfo
from app.core.database import Base
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID as PostgreSQL_UUID
from uuid import UUID, uuid4


class Tag(Base):
    __tablename__ = "tags"
    id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True), primary_key=True, default=uuid4, nullable=False
    )
    name: Mapped[str] = mapped_column(
        String(50), nullable=False, unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    color: Mapped[str] = mapped_column(String(7), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=True)

