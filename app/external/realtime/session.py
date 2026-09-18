"""Typed access to per-connection Socket.IO sessions."""

import socketio

from app.common.schemas.realtime import SocketSession


class SocketSessionNotFoundError(LookupError):
    """Raised when an event has no authenticated Socket.IO session."""


class SocketSessionService:
    def __init__(self, server: socketio.AsyncServer) -> None:
        self._server = server

    async def save(self, sid: str, session: SocketSession) -> None:
        await self._server.save_session(
            sid,
            session.model_dump(mode="json"),
            namespace="/",
        )

    async def get(self, sid: str) -> SocketSession:
        try:
            data = await self._server.get_session(sid, namespace="/")
            return SocketSession.model_validate(data)
        except (KeyError, TypeError, ValueError) as error:
            raise SocketSessionNotFoundError from error
