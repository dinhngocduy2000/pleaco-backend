from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
import socketio

from app.common.context import AppContext
from app.common.enum.context_actions import LIST_MAPS
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import ForbiddenException, NotFoundException
from app.common.schemas.group import GroupMemberInfo
from app.common.schemas.realtime import SocketSession
from app.common.schemas.user import Credential
from app.external.realtime.handlers import SocketIOHandler
from app.external.realtime.rooms import RoomOperationError, RoomService, Rooms
from app.external.realtime.session import (
    SocketSessionNotFoundError,
    SocketSessionService,
)
from app.repository.map import MapRepository
from app.services.map import MapService
from app.services.map_room import MapRoomAccessError, MapRoomService


def credential(group_id=None) -> Credential:
    return Credential(
        id=uuid4(),
        email="member@example.com",
        status=UserStatus.ACTIVE,
        active_group_id=group_id,
    )


def context(actor=None) -> AppContext:
    return AppContext(trace_id=uuid4(), action=LIST_MAPS, actor=actor)


class FakeSocketServer:
    def __init__(self) -> None:
        self.rooms: dict[str, set[str]] = {}
        self.sessions: dict[str, dict] = {}
        self.emissions: list[tuple[str, dict, str, str]] = []
        self.fail_room_operations = False

    async def enter_room(self, sid, room, namespace="/"):
        if self.fail_room_operations:
            raise RuntimeError("room unavailable")
        self.rooms.setdefault(room, set()).add(sid)

    async def leave_room(self, sid, room, namespace="/"):
        if self.fail_room_operations:
            raise RuntimeError("room unavailable")
        self.rooms.setdefault(room, set()).discard(sid)

    async def emit(self, event, payload, to, namespace="/"):
        self.emissions.append((event, payload, to, namespace))

    async def save_session(self, sid, data, namespace="/"):
        self.sessions[sid] = data

    async def get_session(self, sid, namespace="/"):
        return self.sessions[sid]


class FakeSessionService:
    def __init__(self, session: SocketSession | None = None) -> None:
        self.session = session
        self.saved: dict[str, SocketSession] = {}

    async def save(self, sid, session):
        self.saved[sid] = session

    async def get(self, sid):
        if self.session is None:
            raise SocketSessionNotFoundError
        return self.session


class FakePermissionService:
    def __init__(self, allowed: bool = True, role: GroupRole = GroupRole.GUEST):
        self.allowed = allowed
        self.role = role
        self.calls = []

    async def get_group_member(self, credential, ctx, group_id=None):
        self.calls.append((credential.id, group_id))
        if not self.allowed or group_id is None:
            raise ForbiddenException(message="Member not found")
        now = datetime.now(timezone.utc)
        return GroupMemberInfo(
            member_id=credential.id,
            group_id=group_id,
            role=self.role,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def is_action_executable(role, action, is_owner=False):
        return action == LIST_MAPS


class FakeMapRoomService:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.subscriptions = []
        self.unsubscriptions = []

    async def subscribe(self, **kwargs):
        if self.error is not None:
            raise self.error
        self.subscriptions.append(kwargs)

    async def unsubscribe(self, sid, map_id):
        if self.error is not None:
            raise self.error
        self.unsubscriptions.append((sid, map_id))


def socket_handler(
    session: SocketSession | None,
    *,
    permission_service=None,
    room_service=None,
    map_room_service=None,
) -> SocketIOHandler:
    return SocketIOHandler(
        permission_service=permission_service or FakePermissionService(),
        room_service=room_service or RoomService(FakeSocketServer()),
        session_service=FakeSessionService(session),
        map_room_service=map_room_service or FakeMapRoomService(),
    )


def test_room_names_are_centralized_and_stable() -> None:
    user_id, group_id, map_id = uuid4(), uuid4(), uuid4()

    assert Rooms.user(user_id) == f"user:{user_id}"
    assert Rooms.group(group_id) == f"group:{group_id}"
    assert Rooms.map(map_id) == f"map:{map_id}"


@pytest.mark.asyncio
async def test_room_membership_is_idempotent_and_emit_is_room_scoped() -> None:
    server = FakeSocketServer()
    rooms = RoomService(server)
    map_a, map_b = Rooms.map(uuid4()), Rooms.map(uuid4())

    await rooms.join("sid-a", map_a)
    await rooms.join("sid-a", map_a)
    await rooms.join("sid-b", map_b)
    await rooms.emit("robot.position", {"x": 1}, map_a)
    await rooms.leave("sid-a", map_a)
    await rooms.leave("sid-a", map_a)

    assert server.rooms[map_a] == set()
    assert server.rooms[map_b] == {"sid-b"}
    assert server.emissions == [("robot.position", {"x": 1}, map_a, "/")]


@pytest.mark.asyncio
async def test_room_service_hides_transport_errors() -> None:
    server = FakeSocketServer()
    server.fail_room_operations = True

    with pytest.raises(RoomOperationError):
        await RoomService(server).join("sid", Rooms.map(uuid4()))


@pytest.mark.asyncio
async def test_socket_session_round_trip_is_typed() -> None:
    server = FakeSocketServer()
    sessions = SocketSessionService(server)
    expected = SocketSession(credential=credential(uuid4()))

    await sessions.save("sid", expected)

    assert await sessions.get("sid") == expected
    with pytest.raises(SocketSessionNotFoundError):
        await sessions.get("missing")


@pytest.mark.asyncio
async def test_connection_joins_automatic_user_and_group_rooms(monkeypatch) -> None:
    group_id = uuid4()
    actor = credential(group_id)
    server = FakeSocketServer()
    room_service = RoomService(server)
    session_service = FakeSessionService()
    permission_service = FakePermissionService()
    handler = SocketIOHandler(
        permission_service=permission_service,
        room_service=room_service,
        session_service=session_service,
        map_room_service=FakeMapRoomService(),
    )

    async def validate_cookie_tokens(cls, request, ctx):
        return actor

    monkeypatch.setattr(
        "app.external.realtime.handlers.AuthMiddleware.validate_cookie_tokens",
        classmethod(validate_cookie_tokens),
    )

    await handler.connect("sid", {"asgi.scope": {"type": "http", "headers": []}})

    assert permission_service.calls == [(actor.id, group_id)]
    assert session_service.saved["sid"].credential == actor
    assert server.rooms[Rooms.user(actor.id)] == {"sid"}
    assert server.rooms[Rooms.group(group_id)] == {"sid"}


@pytest.mark.asyncio
async def test_connection_without_active_group_only_joins_user_room(monkeypatch) -> None:
    actor = credential()
    server = FakeSocketServer()
    permission_service = FakePermissionService()
    handler = SocketIOHandler(
        permission_service=permission_service,
        room_service=RoomService(server),
        session_service=FakeSessionService(),
        map_room_service=FakeMapRoomService(),
    )

    async def validate_cookie_tokens(cls, request, ctx):
        return actor

    monkeypatch.setattr(
        "app.external.realtime.handlers.AuthMiddleware.validate_cookie_tokens",
        classmethod(validate_cookie_tokens),
    )

    await handler.connect("sid", {"asgi.scope": {"type": "http", "headers": []}})

    assert permission_service.calls == []
    assert server.rooms == {Rooms.user(actor.id): {"sid"}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_failure", [True, False], ids=["invalid-cookie", "invalid-group"]
)
async def test_connection_rejects_invalid_auth_or_group(monkeypatch, auth_failure) -> None:
    actor = credential(uuid4())
    permission_service = FakePermissionService(allowed=auth_failure)
    handler = socket_handler(
        None,
        permission_service=permission_service,
        room_service=RoomService(FakeSocketServer()),
    )

    async def validate_cookie_tokens(cls, request, ctx):
        if auth_failure:
            raise RuntimeError("invalid token")
        return actor

    monkeypatch.setattr(
        "app.external.realtime.handlers.AuthMiddleware.validate_cookie_tokens",
        classmethod(validate_cookie_tokens),
    )

    with pytest.raises(socketio.exceptions.ConnectionRefusedError):
        await handler.connect("sid", {"asgi.scope": {"type": "http", "headers": []}})


@pytest.mark.asyncio
async def test_map_handler_validates_payload_and_session() -> None:
    actor = credential(uuid4())
    map_room_service = FakeMapRoomService()
    handler = socket_handler(
        SocketSession(credential=actor), map_room_service=map_room_service
    )
    map_id = uuid4()

    assert await handler.subscribe_map("sid", {"mapId": str(map_id)}) == {
        "success": True
    }
    assert map_room_service.subscriptions[0]["map_id"] == map_id
    assert await handler.subscribe_map("sid", {"mapId": "bad-id"}) == {
        "success": False,
        "error": "INVALID_PAYLOAD",
    }
    assert await handler.subscribe_map(
        "sid", {"mapId": str(map_id), "unexpected": True}
    ) == {"success": False, "error": "INVALID_PAYLOAD"}
    assert await handler.subscribe_map("sid", {"map_id": str(map_id)}) == {
        "success": False,
        "error": "INVALID_PAYLOAD",
    }
    assert await socket_handler(None).subscribe_map("sid", {"mapId": str(map_id)}) == {
        "success": False,
        "error": "AUTHENTICATION_REQUIRED",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service_error", "expected_code"),
    [
        (MapRoomAccessError("GROUP_REQUIRED"), "GROUP_REQUIRED"),
        (MapRoomAccessError("MAP_NOT_FOUND"), "MAP_NOT_FOUND"),
        (MapRoomAccessError("MAP_ACCESS_DENIED"), "MAP_ACCESS_DENIED"),
        (RoomOperationError(), "ROOM_OPERATION_FAILED"),
        (RuntimeError(), "INTERNAL_ERROR"),
    ],
)
async def test_map_handler_returns_stable_error_codes(service_error, expected_code) -> None:
    actor = credential(uuid4())
    handler = socket_handler(
        SocketSession(credential=actor),
        map_room_service=FakeMapRoomService(service_error),
    )

    result = await handler.subscribe_map("sid", {"mapId": str(uuid4())})

    assert result == {"success": False, "error": expected_code}


@pytest.mark.asyncio
async def test_unsubscribe_preserves_automatic_rooms() -> None:
    actor = credential(uuid4())
    server = FakeSocketServer()
    room_service = RoomService(server)
    handler = socket_handler(
        SocketSession(credential=actor),
        room_service=room_service,
        map_room_service=MapRoomService(
            map_service=SimpleNamespace(), room_service=room_service
        ),
    )
    map_id = uuid4()
    await room_service.join("sid", Rooms.user(actor.id))
    await room_service.join("sid", Rooms.group(actor.active_group_id))
    await room_service.join("sid", Rooms.map(map_id))

    result = await handler.unsubscribe_map("sid", {"mapId": str(map_id)})

    assert result == {"success": True}
    assert server.rooms[Rooms.map(map_id)] == set()
    assert server.rooms[Rooms.user(actor.id)] == {"sid"}
    assert server.rooms[Rooms.group(actor.active_group_id)] == {"sid"}


class MapRepositoryStub:
    def __init__(self, exists: bool) -> None:
        self.exists = exists
        self.calls = []

    async def exists_by_id_and_group(self, **kwargs):
        self.calls.append((kwargs["map_id"], kwargs["group_id"]))
        return self.exists


def map_service(exists: bool, *, member: bool = True):
    repository = MapRepositoryStub(exists)
    registry = SimpleNamespace(
        map_repo=lambda: repository,
        transaction_wrapper=lambda callback: callback(SimpleNamespace()),
    )
    return MapService(registry, FakePermissionService(allowed=member)), repository


@pytest.mark.asyncio
async def test_map_access_is_group_scoped_and_cross_group_is_not_found() -> None:
    group_id, map_id = uuid4(), uuid4()
    actor = credential(group_id)
    service, repository = map_service(False)

    with pytest.raises(NotFoundException, match="Map not found"):
        await service.ensure_realtime_access(
            map_id=map_id,
            group_id=group_id,
            credential=actor,
            ctx=context(actor.id),
        )

    assert repository.calls == [(map_id, group_id)]


class ExistsResultStub:
    def __init__(self, value: bool) -> None:
        self.value = value

    def scalar_one(self) -> bool:
        return self.value


class ExistsSessionStub:
    def __init__(self, value: bool) -> None:
        self.value = value
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return ExistsResultStub(self.value)


@pytest.mark.asyncio
async def test_map_repository_existence_query_is_group_scoped() -> None:
    session = ExistsSessionStub(True)
    map_id, group_id = uuid4(), uuid4()

    found = await MapRepository().exists_by_id_and_group(
        session=session,
        map_id=map_id,
        group_id=group_id,
        ctx=context(),
    )

    assert found is True
    statement = str(session.statement)
    assert "maps.id" in statement
    assert "maps.group_id" in statement


@pytest.mark.asyncio
async def test_map_room_service_maps_access_failures_and_joins_authorized_map() -> None:
    group_id, map_id = uuid4(), uuid4()
    actor = credential(group_id)
    session = SocketSession(credential=actor)
    server = FakeSocketServer()
    allowed_map_service, _ = map_service(True)
    service = MapRoomService(allowed_map_service, RoomService(server))

    await service.subscribe("sid", map_id, session, context(actor.id))
    assert server.rooms[Rooms.map(map_id)] == {"sid"}

    missing_group = SocketSession(credential=credential())
    with pytest.raises(MapRoomAccessError, match="GROUP_REQUIRED"):
        await service.subscribe("sid", map_id, missing_group, context())

    denied_map_service, _ = map_service(True, member=False)
    denied = MapRoomService(denied_map_service, RoomService(server))
    with pytest.raises(MapRoomAccessError, match="MAP_ACCESS_DENIED"):
        await denied.subscribe("sid-2", map_id, session, context(actor.id))
