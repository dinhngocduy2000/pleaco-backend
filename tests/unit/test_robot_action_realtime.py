from unittest.mock import AsyncMock

import pytest

from app.external.realtime.robot_action import RobotActionRealtimePublisher
from app.external.realtime.rooms import RoomOperationError


@pytest.mark.asyncio
async def test_position_is_sent_only_to_its_map_room():
    rooms = AsyncMock()
    publisher = RobotActionRealtimePublisher(rooms)
    position = {
        "map_id": "map-1", "robot_id": "robot-1", "group_id": "group-1",
        "command_id": "command-1", "x": 1.0, "y": 2.0,
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    await publisher.publish(position)
    rooms.emit.assert_awaited_once_with(
        "map.robot.position",
        {"map_id": "map-1", "robot_id": "robot-1", "position": {
            "x": 1.0, "y": 2.0, "updated_at": "2026-01-01T00:00:00+00:00",
        }},
        "map:map-1",
    )


@pytest.mark.asyncio
async def test_delivery_failure_is_best_effort():
    rooms = AsyncMock()
    rooms.emit.side_effect = RoomOperationError()
    await RobotActionRealtimePublisher(rooms).publish({
        "map_id": "map-1", "robot_id": "robot-1", "group_id": "group-1",
        "command_id": "command-1", "x": 1.0, "y": 2.0,
        "updated_at": "2026-01-01T00:00:00+00:00",
    })
