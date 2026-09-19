"""Thin Socket.IO connection and map-event adapters."""

from typing import Any
from uuid import uuid4

import socketio
from pydantic import ValidationError
from starlette.requests import HTTPConnection

from app.common.context import AppContext
from app.common.enum.context_actions import AUTHENTICATE_USER, LIST_MAPS
from app.common.middleware.auth_middleware import AuthMiddleware
from app.common.middleware.logger import Logger
from app.common.schemas.realtime import MapSubscriptionRequest, SocketSession
from app.core.rbac.permissions import PermissionService
from app.external.realtime.rooms import RoomOperationError, RoomService, Rooms
from app.external.realtime.session import (
    SocketSessionNotFoundError,
    SocketSessionService,
)
from app.services.map_room import MapRoomAccessError, MapRoomService

logger = Logger()


def _success() -> dict[str, bool]:
    """Return the stable acknowledgement for a successful client event."""
    return {"success": True}


def _failure(code: str) -> dict[str, bool | str]:
    """Return a failure acknowledgement without exposing internal details.

    Args:
        code: Stable public error code understood by the frontend.

    Returns:
        A Socket.IO acknowledgement containing ``success`` and ``error``.
    """
    return {"success": False, "error": code}


class SocketIOHandler:
    """Adapt Socket.IO lifecycle and map events to Pleco application services.

    The handler owns transport parsing, session lookup, acknowledgement
    formatting, and exception translation. Domain authorization remains in
    services, while room mechanics remain in realtime adapters.
    """

    def __init__(
        self,
        permission_service: PermissionService,
        room_service: RoomService,
        session_service: SocketSessionService,
        map_room_service: MapRoomService,
    ) -> None:
        """Initialize the Socket.IO event adapter.

        Args:
            permission_service: Resolves current active-group membership during
                the connection handshake.
            room_service: Joins automatic user and group rooms.
            session_service: Stores trusted identity for later socket events.
            map_room_service: Authorizes dynamic map membership changes.
        """
        self._permission_service = permission_service
        self._room_service = room_service
        self._session_service = session_service
        self._map_room_service = map_room_service

    async def connect(
        self, sid: str, environ: dict[str, Any], auth: Any = None
    ) -> None:
        """Authenticate a connection and join its automatic audience rooms.

        Authentication reuses the existing HTTP-only cookie flow. Identity from
        the optional Socket.IO ``auth`` payload is intentionally ignored. An
        authenticated user always joins ``user:{user_id}``; a user with a valid
        active group also joins ``group:{group_id}``.

        Args:
            sid: Socket.IO connection identifier assigned by the server.
            environ: Engine.IO request environment containing the ASGI scope.
            auth: Optional client handshake payload; unused for authentication.

        Raises:
            socketio.exceptions.ConnectionRefusedError: If cookie validation,
                active-group membership validation, session persistence, or an
                automatic room operation fails.
        """
        del auth
        ctx = AppContext(trace_id=uuid4(), action=AUTHENTICATE_USER)
        try:
            # HTTPConnection exposes cookies for both HTTP and WebSocket ASGI
            # scopes without introducing a Socket.IO-specific auth mechanism.
            scope = environ.get("asgi.scope")
            if not isinstance(scope, dict):
                raise ValueError("Missing ASGI scope")
            credential = await AuthMiddleware.validate_cookie_tokens(
                HTTPConnection(scope), ctx
            )
            # A group recorded in a token may have become stale after issuance,
            # so membership is verified again before joining its automatic room.
            if credential.active_group_id is not None:
                await self._permission_service.get_group_member(
                    credential=credential,
                    group_id=credential.active_group_id,
                    ctx=ctx,
                )

            await self._session_service.save(
                sid, SocketSession(credential=credential)
            )
            await self._room_service.join(sid, Rooms.user(credential.id))
            if credential.active_group_id is not None:
                await self._room_service.join(
                    sid, Rooms.group(credential.active_group_id)
                )
        except Exception as error:
            logger.warning(
                msg="Rejected unauthorized Socket.IO connection", context=ctx
            )
            raise socketio.exceptions.ConnectionRefusedError(
                {"error": "AUTHENTICATION_REQUIRED"}
            ) from error

        logger.info(msg=f"Authorized Socket.IO connection sid={sid}", context=ctx)

    async def disconnect(self, sid: str, reason: str | None = None) -> None:
        """Record a disconnect while Socket.IO performs session/room cleanup.

        Args:
            sid: Socket.IO connection identifier being removed.
            reason: Protocol-provided disconnect reason, when available.
        """
        logger.info(msg=f"Socket.IO disconnected sid={sid}; reason={reason}")

    async def subscribe_map(self, sid: str, data: Any) -> dict[str, bool | str]:
        """Handle an authorized ``map.subscribe`` event.

        Args:
            sid: Socket.IO connection identifier requesting membership.
            data: Untrusted event payload expected to contain ``mapId``.

        Returns:
            A success acknowledgement, or a stable failure code for missing
            authentication, invalid input, denied access, or room failures.
        """
        # Resolve identity before parsing resource input so client-supplied data
        # never decides which user or group is used for authorization.
        try:
            session = await self._session_service.get(sid)
        except SocketSessionNotFoundError:
            return _failure("AUTHENTICATION_REQUIRED")

        try:
            request = MapSubscriptionRequest.model_validate(data)
        except ValidationError:
            return _failure("INVALID_PAYLOAD")

        ctx = AppContext(
            trace_id=uuid4(), action=LIST_MAPS, actor=session.credential.id
        )
        try:
            await self._map_room_service.subscribe(
                sid=sid,
                map_id=request.map_id,
                session=session,
                ctx=ctx,
            )
        except MapRoomAccessError as error:
            return _failure(error.code)
        except RoomOperationError:
            logger.exception(msg="Failed to join Socket.IO map room", context=ctx)
            return _failure("ROOM_OPERATION_FAILED")
        except Exception:
            logger.exception(
                msg="Unexpected Socket.IO map subscription failure", context=ctx
            )
            return _failure("INTERNAL_ERROR")
        return _success()

    async def unsubscribe_map(self, sid: str, data: Any) -> dict[str, bool | str]:
        """Handle an idempotent ``map.unsubscribe`` event.

        Args:
            sid: Socket.IO connection identifier leaving the map audience.
            data: Untrusted event payload expected to contain ``mapId``.

        Returns:
            A success acknowledgement, or a stable failure code for missing
            authentication, invalid input, or room-operation failures.
        """
        try:
            session = await self._session_service.get(sid)
        except SocketSessionNotFoundError:
            return _failure("AUTHENTICATION_REQUIRED")

        try:
            request = MapSubscriptionRequest.model_validate(data)
        except ValidationError:
            return _failure("INVALID_PAYLOAD")

        ctx = AppContext(
            trace_id=uuid4(), action=LIST_MAPS, actor=session.credential.id
        )
        try:
            await self._map_room_service.unsubscribe(sid, request.map_id)
        except RoomOperationError:
            logger.exception(msg="Failed to leave Socket.IO map room", context=ctx)
            return _failure("ROOM_OPERATION_FAILED")
        except Exception:
            logger.exception(
                msg="Unexpected Socket.IO map unsubscription failure", context=ctx
            )
            return _failure("INTERNAL_ERROR")
        return _success()


def register_socketio_handlers(
    server: socketio.AsyncServer, handler: SocketIOHandler
) -> None:
    """Bind Pleco event handlers to the default Socket.IO namespace.

    Args:
        server: Shared process-wide Socket.IO server.
        handler: Fully initialized transport adapter for connection and map
            events.
    """
    server.on("connect", handler.connect, namespace="/")
    server.on("disconnect", handler.disconnect, namespace="/")
    server.on("map.subscribe", handler.subscribe_map, namespace="/")
    server.on("map.unsubscribe", handler.unsubscribe_map, namespace="/")
