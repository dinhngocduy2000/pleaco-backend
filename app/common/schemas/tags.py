from datetime import datetime
from typing import Optional
from uuid import UUID
from pydantic import BaseModel, Field


class TagInfo(BaseModel):
    id: UUID = Field(None, description="Tag's id")
    name: str = Field(None, description="Tag's name")
    color: str = Field(None, description="Tag's color")
    description: Optional[str] = Field(None, description="Tag's description")
    created_at: datetime = Field(None, description="Tag's created at")
    updated_at: datetime = Field(None, description="Tag's updated at")
