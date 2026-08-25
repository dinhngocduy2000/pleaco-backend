from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TagCreateDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    group_id: UUID = Field(..., description="Group that owns the tag")
    name: str = Field(..., min_length=1, max_length=50, description="Tag name")
    description: str | None = Field(
        None, max_length=255, description="Optional tag description"
    )
    color: str = Field(
        ...,
        pattern=r"^#[0-9A-Fa-f]{6}$",
        description="Hexadecimal tag color in #RRGGBB format",
    )


class TagInfo(BaseModel):
    id: UUID = Field(..., description="Tag's id")
    name: str = Field(..., description="Tag's name")
    color: str = Field(..., description="Tag's color")
    description: str | None = Field(None, description="Tag's description")
    created_at: datetime = Field(..., description="Tag's created at")
    updated_at: datetime = Field(..., description="Tag's updated at")


class TagListInfo(BaseModel):
    id: UUID = Field(..., description="Tag's id")
    name: str = Field(..., description="Tag's name")
    color: str = Field(..., description="Tag's color")
