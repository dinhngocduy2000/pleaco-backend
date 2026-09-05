from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.common.enum.map import MapBoundarySource, MapStatus
from app.common.schemas.geometry import PolygonGeometry
from app.common.schemas.common import PaginationBaseRequest
from app.common.schemas.tags import TagInfo, TagListInfo


class MapCreateDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    group_id: UUID = Field(..., description="Group that owns the map")
    name: str = Field(..., min_length=1, description="Map name")
    description: str | None = Field(None, description="Optional map description")
    dimension_x: Decimal = Field(..., description="Map X dimension")
    dimension_y: Decimal = Field(..., description="Map Y dimension")
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
    dimension_x: Decimal
    dimension_y: Decimal
    robot_ids: list[UUID] = Field(default_factory=list)
    tags: list[TagInfo] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class MapBoundarySaveDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    map_id: UUID
    source: MapBoundarySource = MapBoundarySource.DIMENSIONS
    geometry: PolygonGeometry | None = None

    @model_validator(mode="after")
    def geometry_required_for_custom_sources(self) -> "MapBoundarySaveDTO":
        if self.source != MapBoundarySource.DIMENSIONS and self.geometry is None:
            raise ValueError("Geometry is required for CUSTOM and TEACH_MODE")
        return self


class MapBoundaryInfo(BaseModel):
    id: UUID
    map_id: UUID
    source: MapBoundarySource
    geometry: PolygonGeometry
    created_at: datetime
    updated_at: datetime


class MapOrderDirection(str, Enum):
    ASC = "asc"
    DESC = "desc"


class MapListQuery(PaginationBaseRequest):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    search: str | None = Field(
        None, min_length=1, description="Case-insensitive map name search"
    )
    status: MapStatus | None = Field(None, description="Map assignment status")
    tag_ids: list[UUID] | None = Field(
        None, description="Return maps with at least one of these tag IDs"
    )
    order_direction: MapOrderDirection = Field(
        MapOrderDirection.DESC, description="Creation-date ordering direction"
    )

    @field_validator("tag_ids")
    @classmethod
    def tag_ids_must_be_unique(cls, tag_ids: list[UUID] | None) -> list[UUID] | None:
        if tag_ids is not None and len(tag_ids) != len(set(tag_ids)):
            raise ValueError("Tag identifiers must be unique")
        return tag_ids


class MapListInfo(BaseModel):
    id: UUID
    name: str
    description: str | None
    status: MapStatus
    tags: list[TagListInfo] = Field(default_factory=list)
    dimension_x: Decimal
    dimension_y: Decimal
    updated_at: datetime
    geometry: PolygonGeometry | None = Field(
        None, description="Boundary polygon in local map coordinates, or null if unset"
    )
