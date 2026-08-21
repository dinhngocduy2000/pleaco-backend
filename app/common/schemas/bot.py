from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress, field_validator

from app.common.enum.robot import (
    RobotConnectionStatus,
    RobotModel,
    RobotOperationalStatus,
)
from app.common.schemas.tags import TagInfo


class BotCreateDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    group_id: UUID = Field(..., description="Group that owns the bot")
    name: str = Field(..., min_length=1, description="Bot name")
    serial_num: str = Field(
        ..., min_length=1, description="Manufacturer serial number"
    )
    model: RobotModel = Field(..., description="Bot model")
    map_id: UUID | None = Field(None, description="Reserved map assignment")
    ip_address: IPvAnyAddress | None = Field(
        None, description="Bot IPv4 or IPv6 address"
    )
    tags: list[UUID] = Field(..., description="Existing tag identifiers")

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
    tags: list[TagInfo]
    created_at: datetime
    updated_at: datetime
