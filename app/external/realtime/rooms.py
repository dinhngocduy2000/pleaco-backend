"""Generic Socket.IO room naming and transport operations."""

from uuid import UUID

import socketio


class RoomOperationError(RuntimeError):
    """Raised when the Socket.IO server cannot complete a room operation."""


class Rooms:
    @staticmethod
    def user(user_id: UUID | str) -> str:
        return f"user:{user_id}"

    @staticmethod
    def group(group_id: UUID | str) -> str:
        return f"group:{group_id}"

    @staticmethod
    def map(map_id: UUID | str) -> str:
        return f"map:{map_id}"


class RoomService:
    def __init__(self, server: socketio.AsyncServer) -> None:
        self._server = server

    async def join(self, sid: str, room: str) -> None:
        try:
            await self._server.enter_room(sid, room, namespace="/")
        except Exception as error:
            raise RoomOperationError from error

    async def leave(self, sid: str, room: str) -> None:
        try:
            await self._server.leave_room(sid, room, namespace="/")
        except Exception as error:
            raise RoomOperationError from error

    async def emit(self, event: str, payload: dict, room: str) -> None:
        try:
            await self._server.emit(event, payload, to=room, namespace="/")
        except Exception as error:
            raise RoomOperationError from error
