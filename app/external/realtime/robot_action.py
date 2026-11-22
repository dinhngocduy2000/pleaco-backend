"""Socket.IO delivery of confirmed robot positions."""

from app.common.middleware.logger import Logger
from app.common.schemas.robot_action import RobotPositionRecord
from app.external.realtime.rooms import RoomOperationError, RoomService, Rooms

logger = Logger()


class RobotActionRealtimePublisher:
    """Publish committed positions to the authorized map audience."""

    def __init__(self, room_service: RoomService) -> None:
        self._room_service = room_service

    async def publish(self, position: RobotPositionRecord) -> None:
        payload = {
            "map_id": position["map_id"],
            "robot_id": position["robot_id"],
            "position": {key: position[key] for key in ("x", "y", "updated_at")},
        }
        try:
            await self._room_service.emit("map.robot.position", payload, Rooms.map(position["map_id"]))
        except RoomOperationError:
            logger.exception(msg=f"Unable to publish position for robot {position['robot_id']}")
