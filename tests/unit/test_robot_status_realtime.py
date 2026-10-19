from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.common.enum.robot import RobotConnectionStatus, RobotOperationalStatus
from app.external.realtime.robot_status import RobotStatusRealtimePublisher
from app.external.realtime.rooms import RoomOperationError, Rooms


class FakeRoomService:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.emissions: list[tuple[str, dict, str | list[str]]] = []

    async def emit(self, event: str, payload: dict, room: str | list[str]) -> None:
        if self.fail:
            raise RoomOperationError
        self.emissions.append((event, payload, room))


@pytest.mark.asyncio
async def test_publishes_json_safe_status_to_trusted_group_room():
    group_id = uuid4()
    robot_id = uuid4()
    last_seen_at = datetime.now(timezone.utc)
    rooms = FakeRoomService()
    publisher = RobotStatusRealtimePublisher(rooms)  # type: ignore[arg-type]

    await publisher.publish(
        group_id=group_id,
        map_id=None,
        robot_id=robot_id,
        ip_address="192.168.10.24",
        connection_status=RobotConnectionStatus.ONLINE,
        operational_status=RobotOperationalStatus.IDLE,
        last_seen_at=last_seen_at,
    )

    assert rooms.emissions == [
        (
            "robot.status",
            {
                "robot_id": str(robot_id),
                "ip_address": "192.168.10.24",
                "connection_status": RobotConnectionStatus.ONLINE.value,
                "operational_status": RobotOperationalStatus.IDLE.value,
                "last_seen_at": last_seen_at.isoformat().replace("+00:00", "Z"),
            },
            Rooms.group(group_id),
        )
    ]


@pytest.mark.asyncio
async def test_publishes_once_to_group_and_assigned_map():
    group_id = uuid4()
    map_id = uuid4()
    rooms = FakeRoomService()
    publisher = RobotStatusRealtimePublisher(rooms)  # type: ignore[arg-type]

    await publisher.publish(
        group_id=group_id,
        map_id=map_id,
        robot_id=uuid4(),
        ip_address=None,
        connection_status=RobotConnectionStatus.OFFLINE,
        operational_status=RobotOperationalStatus.IDLE,
        last_seen_at=None,
    )

    assert len(rooms.emissions) == 1
    assert rooms.emissions[0][0] == "robot.status"
    assert rooms.emissions[0][2] == [Rooms.group(group_id), Rooms.map(map_id)]
    assert rooms.emissions[0][1]["connection_status"] == "OFFLINE"


@pytest.mark.asyncio
async def test_delivery_failure_does_not_escape_to_rabbitmq_consumer():
    publisher = RobotStatusRealtimePublisher(  # type: ignore[arg-type]
        FakeRoomService(fail=True)
    )

    await publisher.publish(
        group_id=uuid4(),
        map_id=uuid4(),
        robot_id=uuid4(),
        ip_address=None,
        connection_status=RobotConnectionStatus.OFFLINE,
        operational_status=RobotOperationalStatus.IDLE,
        last_seen_at=datetime.now(timezone.utc),
    )
