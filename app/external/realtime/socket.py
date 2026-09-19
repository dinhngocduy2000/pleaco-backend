"""Shared Socket.IO server and ASGI integration."""

from collections.abc import Awaitable, Callable

import socketio

from app.core.config import settings


def _allowed_origins() -> list[str]:
    """Resolve the browser origins allowed to open Socket.IO connections.

    Returns:
        Normalized configured CORS origins, or ``FRONTEND_URL`` when no
        explicit backend origin allowlist is configured.
    """
    configured = [str(origin).rstrip("/") for origin in settings.BACKEND_CORS_ORIGINS]
    return configured or [settings.FRONTEND_URL.rstrip("/")]


# One server is shared by all realtime adapters in this Python process. A
# multi-process deployment will require a distributed Socket.IO client manager.
socket_server = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=_allowed_origins(),
    cors_credentials=True,
)


def create_socketio_app(
    other_asgi_app: Callable[..., Awaitable[None]],
) -> socketio.ASGIApp:
    """Wrap FastAPI and the shared Socket.IO server in one ASGI application.

    The wrapper handles Engine.IO/Socket.IO traffic at ``/api/v1/ws`` and
    forwards every other ASGI request, including HTTP routes and native
    WebSockets, to the supplied FastAPI application.

    Args:
        other_asgi_app: FastAPI or another ASGI application receiving all
            non-Socket.IO traffic.

    Returns:
        The combined ASGI application exported to Uvicorn.
    """
    return socketio.ASGIApp(
        socket_server,
        other_asgi_app=other_asgi_app,
        socketio_path="api/v1/ws",
    )
