"""Authorization and workflow for dynamic Socket.IO map rooms."""

from uuid import UUID

from app.common.context import AppContext
from app.common.exceptions import ForbiddenException, NotFoundException
from app.common.schemas.realtime import SocketSession
from app.external.realtime.rooms import RoomService, Rooms
from app.services.map import MapService


class MapRoomAccessError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class MapRoomService:
    def __init__(self, map_service: MapService, room_service: RoomService) -> None:
        self._map_service = map_service
        self._room_service = room_service

    async def subscribe(
        self,
        sid: str,
        map_id: UUID,
        session: SocketSession,
        ctx: AppContext,
    ) -> None:
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
        await self._room_service.leave(sid, Rooms.map(map_id))
