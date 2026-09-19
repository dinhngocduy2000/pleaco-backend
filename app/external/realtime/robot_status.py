"""Socket.IO delivery for group-scoped robot status changes."""

from datetime import datetime
from uuid import UUID

from app.common.enum.robot import RobotConnectionStatus, RobotOperationalStatus
from app.common.middleware.logger import Logger
from app.common.schemas.robot_status import RobotStatusRealtimePayload
from app.external.realtime.rooms import RoomOperationError, RoomService, Rooms

logger = Logger()


class RobotStatusRealtimePublisher:
    """Publish reconciled robot status to its persisted group's room."""

    event_name = "robot.status"

    def __init__(self, room_service: RoomService) -> None:
        self._room_service = room_service

    async def publish(
        self,
        *,
        group_id: UUID,
        robot_id: UUID,
        ip_address: str | None,
        connection_status: RobotConnectionStatus | str,
        operational_status: RobotOperationalStatus | str,
        last_seen_at: datetime,
    ) -> None:
        """Emit one JSON-safe status payload to the trusted group room.

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
        try:
            await self._room_service.emit(
                self.event_name,
                payload,
                Rooms.group(group_id),
            )
        except RoomOperationError:
            logger.exception(
                msg=(
                    "Unable to deliver robot status through Socket.IO "
                    f"for robot {robot_id} in group {group_id}"
                )
            )
