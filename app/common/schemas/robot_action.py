"""Version-one robot command and acknowledgement contracts."""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal, TypeAlias, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

from app.common.enum.direction import Direction
from app.common.enum.robot import RobotConnectionStatus, RobotOperationalStatus

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
RobotCommandStatusValue: TypeAlias = Literal[
    "QUEUED", "AWAITING_ACK", "SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"
]


class RobotCoordinates(TypedDict):
    x: float
    y: float


class RobotMovementCoordinates(TypedDict):
    """Origin and target coordinates in meters for movement validation."""

    origin_x: Decimal
    origin_y: Decimal
    target_x: Decimal
    target_y: Decimal


class RobotCleaningActionRecord(TypedDict):
    type: Literal["CLEANING"]
    is_start: bool
    is_finish: bool
    direction: Literal["NORTH", "SOUTH", "EAST", "WEST"]
    distance: str


class RobotCommandRecord(TypedDict):
    robot_id: str
    command_id: str
    attempt: int
    action: RobotCleaningActionRecord
    group_id: str
    map_id: str
    origin: RobotCoordinates
    target: RobotCoordinates
    status: RobotCommandStatusValue
    created_at: str
    updated_at: str
    deadline: str
    failure_reason: str | None


class RobotPositionRecord(RobotCoordinates):
    robot_id: str
    group_id: str
    map_id: str
    command_id: str
    updated_at: str


class RobotActionContext(TypedDict):
    robot_id: UUID
    group_id: UUID
    map_id: UUID | None
    connection_status: RobotConnectionStatus
    operational_status: RobotOperationalStatus
    dimension_x: Decimal | None
    dimension_y: Decimal | None
    dock_x: float | None
    dock_y: float | None


class ActionType(str, Enum):
    CLEANING = "CLEANING"


class BaseAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ActionType
    is_start: StrictBool
    is_finish: StrictBool


class CleaningAction(BaseAction):
    type: Literal[ActionType.CLEANING] = ActionType.CLEANING
    direction: Direction
    distance: Annotated[str, Field(pattern=r"^(?:0|[1-9]\d*)(?:\.\d+)?m$", max_length=64)] = "1m"

    @field_validator("distance")
    @classmethod
    def positive_distance(cls, value: str) -> str:
        if Decimal(value[:-1]) <= 0:
            raise ValueError("Distance must be positive")
        return value


class RobotCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    robot_id: UUID
    command_id: UUID
    attempt: Annotated[StrictInt, Field(ge=1, le=4)]
    action: CleaningAction


class RobotAcknowledgement(RobotCommand):
    ack: StrictBool


class RobotCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: CleaningAction


class CommandStatus(str, Enum):
    QUEUED = "QUEUED"
    AWAITING_ACK = "AWAITING_ACK"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


class RobotCommandInfo(RobotCommand):
    status: CommandStatus
    created_at: datetime
    updated_at: datetime
    deadline: datetime
    failure_reason: str | None = None


class RobotPosition(BaseModel):
    robot_id: UUID
    group_id: UUID
    map_id: UUID
    x: float
    y: float
    command_id: UUID
    updated_at: datetime
