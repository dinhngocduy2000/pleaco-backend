"""Authenticated WebSocket endpoint for group-scoped robot updates."""

from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.common.context import AppContext
from app.common.middleware.auth_middleware import AuthMiddleware
from app.common.middleware.logger import Logger

logger = Logger()


class RealtimeRouter:
    def __init__(self, websocket_manager, permission_service) -> None:
        self.router = APIRouter(prefix="/realtime", tags=["Realtime"])
        self._websocket_manager = websocket_manager
        self._permission_service = permission_service
        self.router.add_api_websocket_route("/robots", self.robot_status_socket)

    async def robot_status_socket(self, websocket: WebSocket) -> None:
        ctx = AppContext(trace_id=uuid4(), action="ROBOT_STATUS_WEBSOCKET")
        try:
            credential = await AuthMiddleware._validate_cookie_tokens(websocket, ctx)
            if credential.active_group_id is None:
                logger.warning(msg="Rejected robot-status WebSocket without an active group", context=ctx)
                await websocket.close(code=1008)
                return
            await self._permission_service.get_group_member(credential, ctx)
        except Exception:
            logger.warning(msg="Rejected unauthorized robot-status WebSocket client", context=ctx)
            await websocket.close(code=1008)
            return

        group_id = credential.active_group_id
        logger.info(msg=f"Authorized robot-status WebSocket for group {group_id}", context=ctx)
        await self._websocket_manager.connect(group_id, websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await self._websocket_manager.disconnect(group_id, websocket)
