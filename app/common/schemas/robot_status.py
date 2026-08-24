"""Validated contracts for robot connectivity events."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress, field_validator

from app.common.enum.robot import RobotConnectionStatus, RobotOperationalStatus


RobotStatusEventType = Literal["robot.online", "robot.offline", "robot.status.updated"]


class RobotStatusData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_status: RobotConnectionStatus | None = None
    operational_status: RobotOperationalStatus | None = None


class MqttRobotStatusMessage(BaseModel):
    """Untrusted device message accepted only after topic identity verification."""

    model_config = ConfigDict(extra="forbid")

    message_id: UUID
    robot_id: UUID
    ip_address: IPvAnyAddress | None = None
    event: RobotStatusEventType
    sequence_number: int = Field(ge=0)
    timestamp: datetime
    data: RobotStatusData

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_be_timezone_aware(cls, timestamp: datetime) -> datetime:
        if timestamp.tzinfo is None:
            raise ValueError("timestamp must include a UTC offset")
        return timestamp

    @field_validator("data")
    @classmethod
    def validate_event_status(cls, data: RobotStatusData, info) -> RobotStatusData:
        event = info.data.get("event")
        expected = {
            "robot.online": RobotConnectionStatus.ONLINE,
            "robot.offline": RobotConnectionStatus.OFFLINE,
        }.get(event)
        if expected is not None and data.connection_status != expected:
            raise ValueError(f"{event} must carry connection_status={expected.value}")
        if event == "robot.status.updated" and (
            data.connection_status is None and data.operational_status is None
        ):
            raise ValueError("robot.status.updated must include a status value")
        return data


class RobotStatusEvent(BaseModel):
    """Normalized internal event published after MQTT validation."""

    message_id: UUID
    type: Literal["robot.connection.changed", "robot.status.changed"]
    robot_id: UUID
    ip_address: IPvAnyAddress | None = None
    sequence_number: int = Field(ge=0)
    connection_status: RobotConnectionStatus | None = None
    operational_status: RobotOperationalStatus | None = None
    occurred_at: datetime

    def cache_state(self, *, last_seen_at: datetime) -> dict[str, str | int | None]:
        return {
            "robot_id": str(self.robot_id),
            "ip_address": str(self.ip_address) if self.ip_address is not None else None,
            "connection_status": self.connection_status.value if self.connection_status else None,
            "operational_status": self.operational_status.value if self.operational_status else None,
            "last_seen_at": last_seen_at.isoformat(),
            "last_sequence_number": self.sequence_number,
            "last_message_id": str(self.message_id),
        }
