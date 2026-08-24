"""In-process, group-scoped WebSocket delivery for robot status changes."""

import asyncio
from collections import defaultdict
from uuid import UUID

from fastapi import WebSocket

from app.common.middleware.logger import Logger

logger = Logger()


class RobotStatusWebSocketManager:
    def __init__(self) -> None:
        self._connections: dict[UUID, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, group_id: UUID, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections[group_id].add(websocket)
            connection_count = len(self._connections[group_id])
        logger.info(
            msg=(
                f"Robot-status WebSocket connected for group {group_id}; "
                f"active_connections={connection_count}"
            )
        )

    async def disconnect(self, group_id: UUID, websocket: WebSocket) -> None:
        async with self._lock:
            connections = self._connections.get(group_id)
            if connections is not None:
                connections.discard(websocket)
                if not connections:
                    self._connections.pop(group_id, None)
                connection_count = len(connections)
            else:
                connection_count = 0
        logger.info(
            msg=(
                f"Robot-status WebSocket disconnected for group {group_id}; "
                f"active_connections={connection_count}"
            )
        )

    async def broadcast(self, group_id: UUID, payload: dict) -> None:
        async with self._lock:
            recipients = tuple(self._connections.get(group_id, set()))
        logger.info(
            msg=(
                f"Broadcasting WebSocket event type={payload.get('type')} "
                f"to group {group_id}; recipients={len(recipients)}"
            )
        )
        disconnected: list[WebSocket] = []
        for websocket in recipients:
            try:
                await websocket.send_json(payload)
                logger.info(
                    msg=(
                        f"Delivered WebSocket event type={payload.get('type')} "
                        f"to group {group_id}"
                    )
                )
            except Exception as error:
                disconnected.append(websocket)
                logger.warning(
                    msg=(
                        f"WebSocket delivery failed for group {group_id}: {error}"
                    )
                )
        for websocket in disconnected:
            await self.disconnect(group_id, websocket)
        if disconnected:
            logger.warning(msg="Removed disconnected robot-status WebSocket client(s)")
