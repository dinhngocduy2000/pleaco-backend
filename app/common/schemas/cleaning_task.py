"""Public cleaning-task responses and internal movement queue entries."""

from datetime import datetime
from enum import Enum
from typing import NotRequired, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.common.enum.direction import Direction
from app.common.schemas.cleaning_path import CleaningPathPoint
from app.common.schemas.robot_action import RobotCoordinates


class CleaningMove(TypedDict):
    """One cardinal movement, with coordinates and a decimal meter distance."""

    origin: RobotCoordinates
    target: RobotCoordinates
    direction: Direction
    distance: str


class CleaningTaskStatus(str, Enum):
    """Lifecycle states for a task spanning multiple robot commands."""

    QUEUED = "QUEUED"
    WAITING_INTERVAL = "WAITING_INTERVAL"
    WAITING_OCCUPANCY = "WAITING_OCCUPANCY"
    AWAITING_ACK = "AWAITING_ACK"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class CleaningTaskRecord(TypedDict):
    """Redis task snapshot, including layout and peer checks needed at dispatch."""

    task_id: str
    group_id: str
    map_id: str
    robot_id: str
    status: str
    completed_moves: int
    total_moves: int
    next_waypoint: RobotCoordinates | None
    next_check_at: str | None
    occupancy_retries_used: int
    terminal_reason: str | None
    layout_signature: str
    confirmed_peer_ids: list[str]
    trace_id: NotRequired[str]
    actor_id: NotRequired[str]


class CleaningTaskStartRequest(BaseModel):
    """Select the robot that should clean the map in the URL."""

    model_config = ConfigDict(extra="forbid")

    robot_id: UUID


class CleaningTaskInfo(BaseModel):
    """Public task progress, upcoming waypoint, and any terminal reason."""

    task_id: UUID
    group_id: UUID
    map_id: UUID
    robot_id: UUID
    status: CleaningTaskStatus
    completed_moves: int
    total_moves: int
    occupancy_retries_used: int
    next_waypoint: CleaningPathPoint | None = None
    next_check_at: datetime | None = None
    terminal_reason: str | None = None
