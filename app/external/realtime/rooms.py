"""Generic Socket.IO room naming and transport operations."""

from uuid import UUID

import socketio


class RoomOperationError(RuntimeError):
    """Raised when the Socket.IO server cannot complete a room operation."""


class Rooms:
    """Build canonical room names for each supported realtime audience.

    Centralizing these names prevents producers and subscription handlers from
    accidentally targeting different room formats for the same resource.
    """

    @staticmethod
    def user(user_id: UUID | str) -> str:
        """Return the automatic room for one authenticated user.

        Args:
            user_id: Persistent user identifier.

        Returns:
            A room name in the form ``user:{user_id}``.
        """
        return f"user:{user_id}"

    @staticmethod
    def group(group_id: UUID | str) -> str:
        """Return the automatic room for an authenticated user's active group.

        Args:
            group_id: Persistent group identifier.

        Returns:
            A room name in the form ``group:{group_id}``.
        """
        return f"group:{group_id}"

    @staticmethod
    def map(map_id: UUID | str) -> str:
        """Return the dynamic room for clients viewing a specific map.

        Args:
            map_id: Persistent map identifier.

        Returns:
            A room name in the form ``map:{map_id}``.
        """
        return f"map:{map_id}"


class RoomService:
    """Provide generic room operations over the shared Socket.IO server.

    This transport adapter deliberately contains no map, group, or permission
    rules. Callers must authorize a dynamic subscription before invoking it.
    """

    def __init__(self, server: socketio.AsyncServer) -> None:
        """Initialize the room adapter.

        Args:
            server: Process-wide asynchronous Socket.IO server.
        """
        self._server = server

    async def join(self, sid: str, room: str) -> None:
        """Add one connected socket to a room in the default namespace.

        Joining is idempotent according to Socket.IO room semantics.

        Args:
            sid: Socket.IO connection identifier.
            room: Canonical room name produced by ``Rooms``.

        Raises:
            RoomOperationError: If Socket.IO cannot update room membership.
        """
        try:
            await self._server.enter_room(sid, room, namespace="/")
        except Exception as error:
            raise RoomOperationError from error

    async def leave(self, sid: str, room: str) -> None:
        """Remove one connected socket from a room in the default namespace.

        Args:
            sid: Socket.IO connection identifier.
            room: Canonical room name produced by ``Rooms``.

        Raises:
            RoomOperationError: If Socket.IO cannot update room membership.
        """
        try:
            await self._server.leave_room(sid, room, namespace="/")
        except Exception as error:
            raise RoomOperationError from error

    async def emit(self, event: str, payload: dict, room: str) -> None:
        """Deliver an event to every socket currently in a room.

        Args:
            event: Public Socket.IO event name.
            payload: JSON-compatible event body.
            room: Canonical destination room name.

        Raises:
            RoomOperationError: If Socket.IO cannot emit the event.
        """
        try:
            await self._server.emit(event, payload, to=room, namespace="/")
        except Exception as error:
            raise RoomOperationError from error
