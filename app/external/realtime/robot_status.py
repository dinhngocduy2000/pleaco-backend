"""Socket.IO delivery for robot connectivity changes."""

from datetime import datetime
from uuid import UUID

from app.common.enum.robot import RobotConnectionStatus, RobotOperationalStatus
from app.common.middleware.logger import Logger
from app.common.schemas.robot_status import RobotStatusRealtimePayload
from app.external.realtime.rooms import RoomOperationError, RoomService, Rooms

logger = Logger()


class RobotStatusRealtimePublisher:
    """Publish robot connectivity to its persisted group and assigned map."""

    event_name = "robot.status"

    def __init__(self, room_service: RoomService) -> None:
        self._room_service = room_service

    async def publish(
        self,
        *,
        group_id: UUID,
        map_id: UUID | None,
        robot_id: UUID,
        ip_address: str | None,
        connection_status: RobotConnectionStatus | str,
        operational_status: RobotOperationalStatus | str,
        last_seen_at: datetime | None,
    ) -> None:
        """Emit one JSON-safe status payload to the trusted audience rooms.

        Realtime delivery is best-effort. A failure is logged but not raised so
        an event already reconciled in PostgreSQL is not requeued by RabbitMQ.
        """
        payload = RobotStatusRealtimePayload(
            robot_id=robot_id,
            ip_address=ip_address,
            connection_status=connection_status,
            operational_status=operational_status,
            last_seen_at=last_seen_at,
        ).model_dump(mode="json")
        rooms: str | list[str] = Rooms.group(group_id)
        if map_id is not None:
            rooms = [rooms, Rooms.map(map_id)]
        try:
            await self._room_service.emit(
                self.event_name,
                payload,
                rooms,
            )
            logger.info(
                msg=(
                    f"Socket.IO published {self.event_name} for robot {robot_id} "
                    f"status {payload['connection_status']} to rooms {rooms}"
                )
            )
        except RoomOperationError:
            logger.exception(
                msg=(
                    "Unable to deliver robot status through Socket.IO "
                    f"for robot {robot_id} in group {group_id}"
                )
            )
