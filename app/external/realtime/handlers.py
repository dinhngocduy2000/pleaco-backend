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
    return {"success": True}


def _failure(code: str) -> dict[str, bool | str]:
    return {"success": False, "error": code}


class SocketIOHandler:
    def __init__(
        self,
        permission_service: PermissionService,
        room_service: RoomService,
        session_service: SocketSessionService,
        map_room_service: MapRoomService,
    ) -> None:
        self._permission_service = permission_service
        self._room_service = room_service
        self._session_service = session_service
        self._map_room_service = map_room_service

    async def connect(
        self, sid: str, environ: dict[str, Any], auth: Any = None
    ) -> None:
        del auth
        ctx = AppContext(trace_id=uuid4(), action=AUTHENTICATE_USER)
        try:
            scope = environ.get("asgi.scope")
            if not isinstance(scope, dict):
                raise ValueError("Missing ASGI scope")
            credential = await AuthMiddleware.validate_cookie_tokens(
                HTTPConnection(scope), ctx
            )
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
        logger.info(msg=f"Socket.IO disconnected sid={sid}; reason={reason}")

    async def subscribe_map(self, sid: str, data: Any) -> dict[str, bool | str]:
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
    server.on("connect", handler.connect, namespace="/")
    server.on("disconnect", handler.disconnect, namespace="/")
    server.on("map.subscribe", handler.subscribe_map, namespace="/")
    server.on("map.unsubscribe", handler.unsubscribe_map, namespace="/")
