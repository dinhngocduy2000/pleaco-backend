"""Typed access to per-connection Socket.IO sessions."""

import socketio

from app.common.schemas.realtime import SocketSession


class SocketSessionNotFoundError(LookupError):
    """Raised when an event has no authenticated Socket.IO session."""


class SocketSessionService:
    """Store and restore trusted identity state for each Socket.IO connection.

    Sessions are scoped to a socket ID and the default namespace. They prevent
    later events from accepting user or group identity supplied by the client.
    Socket.IO removes this state when the connection ends.
    """

    def __init__(self, server: socketio.AsyncServer) -> None:
        """Initialize the session adapter.

        Args:
            server: Process-wide asynchronous Socket.IO server.
        """
        self._server = server

    async def save(self, sid: str, session: SocketSession) -> None:
        """Serialize and associate an authenticated session with a socket.

        Args:
            sid: Socket.IO connection identifier.
            session: Validated session containing the authenticated credential.
        """
        await self._server.save_session(
            sid,
            session.model_dump(mode="json"),
            namespace="/",
        )

    async def get(self, sid: str) -> SocketSession:
        """Load and validate the authenticated session for a socket.

        Args:
            sid: Socket.IO connection identifier.

        Returns:
            The typed session saved during the connection handshake.

        Raises:
            SocketSessionNotFoundError: If the session is absent or cannot be
                validated as a ``SocketSession``.
        """
        try:
            data = await self._server.get_session(sid, namespace="/")
            return SocketSession.model_validate(data)
        except (KeyError, TypeError, ValueError) as error:
            raise SocketSessionNotFoundError from error
