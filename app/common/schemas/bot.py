from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress, field_validator

from app.common.enum.robot import (
    RobotConnectionStatus,
    RobotModel,
    RobotOperationalStatus,
)
from app.common.schemas.common import HashMapResponse, PaginationBaseRequest
from app.common.schemas.tags import TagInfo


class BotCreateDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    group_id: UUID = Field(..., description="Group that owns the bot")
    name: str = Field(..., min_length=1, description="Bot name")
    serial_num: str = Field(..., min_length=1, description="Manufacturer serial number")
    model: RobotModel = Field(..., description="Bot model")
    map_id: UUID | None = Field(None, description="Reserved map assignment")
    ip_address: IPvAnyAddress | None = Field(
        None, description="Bot IPv4 or IPv6 address"
    )
    tags: list[UUID] = Field(
        default_factory=list,
        description="Existing tag identifiers",
    )

    @field_validator("tags")
    @classmethod
    def tags_must_be_unique(cls, tags: list[UUID]) -> list[UUID]:
        if len(tags) != len(set(tags)):
            raise ValueError("Tag identifiers must be unique")
        return tags


class BotCreateDomain(BaseModel):
    group_id: UUID
    name: str
    serial_num: str
    model: RobotModel
    ip_address: str | None
    tag_ids: list[UUID]
    map_id: UUID | None = None
    connection_status: RobotConnectionStatus = RobotConnectionStatus.OFFLINE
    operational_status: RobotOperationalStatus = RobotOperationalStatus.IDLE


class BotInfo(BaseModel):
    id: UUID
    group_id: UUID
    map_id: UUID | None
    name: str
    serial_num: str
    model: RobotModel
    ip_address: str | None
    connection_status: RobotConnectionStatus
    operational_status: RobotOperationalStatus
    last_seen_at: datetime | None
    tags: list[TagInfo]
    created_at: datetime
    updated_at: datetime


class BotListQuery(PaginationBaseRequest):
    model_config = ConfigDict(str_strip_whitespace=True)

    group_id: UUID = Field(..., description="Group that owns the bots")
    search: str | None = Field(
        None,
        min_length=1,
        description="Case-insensitive search across bot name and serial number",
    )
    model: RobotModel | None = Field(None, description="Bot model filter")
    operational_status: RobotOperationalStatus | None = Field(
        None, description="Operational-status filter"
    )
    connection_status: RobotConnectionStatus | None = Field(
        None, description="Connection-status filter"
    )
    tag_ids: list[UUID] | None = Field(
        None, description="Return bots with at least one of these tag IDs"
    )

    @field_validator("tag_ids")
    @classmethod
    def tag_ids_must_be_unique(cls, tag_ids: list[UUID] | None) -> list[UUID] | None:
        if tag_ids is not None and len(tag_ids) != len(set(tag_ids)):
            raise ValueError("Tag identifiers must be unique")
        return tag_ids


class BotListInfo(BaseModel):
    id: UUID = Field(..., description="ID of the bot")
    map_name: str | None = Field(None, description="Assigned map name")
    name: str = Field(..., description="Bot name")
    serial_num: str = Field(..., description="Manufacturer serial number")
    model: RobotModel = Field(..., description="Bot model")
    ip_address: str | None = Field(None, description="Bot IP address")
    operational_status: RobotOperationalStatus = Field(
        ..., description="Current operational status"
    )
    created_at: datetime = Field(..., description="Bot creation time")
    connection_status: RobotConnectionStatus = Field(
        ..., description="Current connection status"
    )
    last_seen_at: datetime | None = Field(None, description="Most recent accepted device event")
    tags: list[TagInfo] = Field(
        default_factory=list,
        description="Tags assigned to the bot",
    )


class BotKeyValueInfo(HashMapResponse):
    serial_num: str = Field(..., description="Manufacturer serial number")
