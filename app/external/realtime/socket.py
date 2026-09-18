"""Shared Socket.IO server and ASGI integration."""

from collections.abc import Awaitable, Callable

import socketio

from app.core.config import settings


def _allowed_origins() -> list[str]:
    configured = [str(origin).rstrip("/") for origin in settings.BACKEND_CORS_ORIGINS]
    return configured or [settings.FRONTEND_URL.rstrip("/")]


socket_server = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=_allowed_origins(),
    cors_credentials=True,
)


def create_socketio_app(
    other_asgi_app: Callable[..., Awaitable[None]],
) -> socketio.ASGIApp:
    """Route Socket.IO traffic first and forward all other traffic to FastAPI."""
    return socketio.ASGIApp(
        socket_server,
        other_asgi_app=other_asgi_app,
        socketio_path="socket.io",
    )
