from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.common.enum.map import MapStatus
from app.common.schemas.tags import TagInfo


class MapCreateDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    group_id: UUID = Field(..., description="Group that owns the map")
    name: str = Field(..., min_length=1, description="Map name")
    description: str | None = Field(None, description="Optional map description")
    dimension_x: str = Field(..., min_length=1, description="Map X dimension")
    dimension_y: str = Field(..., min_length=1, description="Map Y dimension")
    robot_ids: list[UUID] = Field(
        default_factory=list, description="Unassigned robots to assign to the map"
    )
    tags: list[UUID] = Field(
        default_factory=list, description="Existing tag identifiers"
    )

    @field_validator("robot_ids", "tags")
    @classmethod
    def identifiers_must_be_unique(cls, identifiers: list[UUID]) -> list[UUID]:
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Identifiers must be unique")
        return identifiers


class MapInfo(BaseModel):
    id: UUID
    group_id: UUID
    name: str
    description: str | None
    status: MapStatus
    dimension_x: str
    dimension_y: str
    robot_ids: list[UUID] = Field(default_factory=list)
    tags: list[TagInfo] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
