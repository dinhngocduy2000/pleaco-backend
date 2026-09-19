"""Authorization and workflow for dynamic Socket.IO map rooms."""

from uuid import UUID

from app.common.context import AppContext
from app.common.exceptions import ForbiddenException, NotFoundException
from app.common.schemas.realtime import SocketSession
from app.external.realtime.rooms import RoomService, Rooms
from app.services.map import MapService


class MapRoomAccessError(RuntimeError):
    """Represent a stable client-facing map subscription failure code."""

    def __init__(self, code: str) -> None:
        """Initialize the subscription error.

        Args:
            code: Stable acknowledgement code returned by the event handler.
        """
        self.code = code
        super().__init__(code)


class MapRoomService:
    """Authorize dynamic map subscriptions before changing room membership.

    The service coordinates domain authorization through ``MapService`` and
    delegates Socket.IO mechanics to ``RoomService``. It does not query the
    database or call the Socket.IO server directly.
    """

    def __init__(self, map_service: MapService, room_service: RoomService) -> None:
        """Initialize the map-room workflow.

        Args:
            map_service: Domain service that validates group-scoped map access.
            room_service: Generic Socket.IO room transport adapter.
        """
        self._map_service = map_service
        self._room_service = room_service

    async def subscribe(
        self,
        sid: str,
        map_id: UUID,
        session: SocketSession,
        ctx: AppContext,
    ) -> None:
        """Authorize a socket and join it to one map's realtime audience.

        The map lookup is scoped to the active group stored in the trusted
        socket session. A cross-group map ID is intentionally reported as not
        found so callers cannot enumerate another group's resources.

        Args:
            sid: Socket.IO connection identifier.
            map_id: Requested map identifier from the validated event payload.
            session: Authenticated server-side socket session.
            ctx: Trace and actor context for authorization and logging.

        Raises:
            MapRoomAccessError: If no group is selected, the map is not visible,
                or current membership no longer permits map access.
            RoomOperationError: If Socket.IO cannot join the authorized room.
        """
        credential = session.credential
        group_id = credential.active_group_id
        if group_id is None:
            raise MapRoomAccessError("GROUP_REQUIRED")

        try:
            await self._map_service.ensure_realtime_access(
                map_id=map_id,
                group_id=group_id,
                credential=credential,
                ctx=ctx,
            )
        except NotFoundException as error:
            raise MapRoomAccessError("MAP_NOT_FOUND") from error
        except ForbiddenException as error:
            raise MapRoomAccessError("MAP_ACCESS_DENIED") from error

        await self._room_service.join(sid, Rooms.map(map_id))

    async def unsubscribe(self, sid: str, map_id: UUID) -> None:
        """Remove a socket from one map room without changing automatic rooms.

        No database lookup is needed because a socket can only remove its own
        membership. Leaving an absent room is idempotent in Socket.IO.

        Args:
            sid: Socket.IO connection identifier.
            map_id: Map whose realtime audience the socket is leaving.

        Raises:
            RoomOperationError: If Socket.IO cannot leave the room.
        """
        await self._room_service.leave(sid, Rooms.map(map_id))
