from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal, TypeVar
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from app.common.enum.robot import RobotConnectionStatus, RobotOperationalStatus
from app.common.schemas.robot_action import JsonValue
from app.common.schemas.robot_status import RobotHeartbeatEvent, RobotStatusEvent
from app.external.realtime.robot_status import RobotStatusRealtimePublisher
from app.external.realtime.rooms import RoomOperationError
from app.services.bot_status import BotStatusService


T = TypeVar("T")


class FakeRedis:
    def __init__(self) -> None:
        self.values = {}
        self.expirations = {}
        self.fail = False

    async def get(self, key: str) -> str | None:
        if self.fail:
            raise ConnectionError("Redis unavailable")
        return self.values.get(key)

    async def set(self, key: str, value: str, expire: int | None = None) -> None:
        if self.fail:
            raise ConnectionError("Redis unavailable")
        self.values[key] = value
        self.expirations[key] = expire

    async def update_robot_status_cache(
        self, robot_id: UUID, sequence: int, state: str, connection_status: str,
        connection_changed: bool, received_at: str, ttl: int,
    ) -> bool:
        if self.fail:
            raise ConnectionError("Redis unavailable")
        key = f"robot:{robot_id}:state"
        current = json.loads(self.values[key]) if key in self.values else None
        if current and current["last_sequence_number"] >= sequence:
            return False
        self.values[key] = state
        freshness = f"robot:{robot_id}:last_seen_at"
        if connection_changed and connection_status == "OFFLINE":
            self.values.pop(freshness, None)
        elif connection_changed and connection_status == "ONLINE":
            if freshness not in self.values or self.values[freshness] < received_at:
                self.values[freshness] = received_at
                self.expirations[freshness] = ttl
        return True


class FakeBotRepository:
    def __init__(self, bot: SimpleNamespace) -> None:
        self.bot = bot
        self.update_calls = 0

    async def get_by_id_for_update(self, _session: object, _bot_id: UUID) -> SimpleNamespace:
        return self.bot

    async def update_status(self, _session: object, bot: SimpleNamespace, **kwargs: object) -> SimpleNamespace:
        self.update_calls += 1
        bot.ip_address = kwargs["ip_address"]
        bot.connection_status = kwargs["connection_status"]
        bot.operational_status = kwargs["operational_status"]
        if kwargs["last_seen_at"] is not None:
            bot.last_seen_at = kwargs["last_seen_at"]
        bot.last_sequence_number = kwargs["last_sequence_number"]
        bot.last_message_id = kwargs["last_message_id"]
        return bot


class FakeRegistry:
    def __init__(self, bot: SimpleNamespace, redis: FakeRedis | None = None) -> None:
        self._redis_client = redis or FakeRedis()
        self._bot_repository = FakeBotRepository(bot)
        self.lock = asyncio.Lock()

    def bot_repo(self) -> FakeBotRepository:
        return self._bot_repository

    async def transaction_wrapper(self, callback: Callable[[object], Awaitable[T]]) -> T:
        async with self.lock:
            return await callback(object())


class FakeRealtimePublisher:
    def __init__(self) -> None:
        self.events = []

    async def publish(self, **kwargs: object) -> None:
        self.events.append(kwargs)


class FailingRoomService:
    async def emit(self, _event: str, _payload: dict[str, JsonValue], _rooms: str | list[str]) -> None:
        raise RoomOperationError("Socket.IO unavailable")


def event(robot_id: UUID, sequence: int = 2, *, status: RobotConnectionStatus = RobotConnectionStatus.ONLINE,
          operational: RobotOperationalStatus | None = None, kind: Literal["robot.connection.changed", "robot.status.changed"] = "robot.connection.changed") -> RobotStatusEvent:
    return RobotStatusEvent(
        message_id=uuid4(), type=kind, robot_id=robot_id,
        ip_address="192.168.10.24", sequence_number=sequence,
        connection_status=status, operational_status=operational,
        occurred_at=datetime.now(timezone.utc),
    )


def robot(robot_id: UUID, *, status: RobotConnectionStatus = RobotConnectionStatus.OFFLINE, map_id: UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=robot_id, group_id=uuid4(), map_id=map_id, ip_address="192.168.10.24",
        connection_status=status, operational_status=RobotOperationalStatus.IDLE,
        last_seen_at=None, last_sequence_number=1, last_message_id=uuid4(),
    )


def heartbeat(robot_id: UUID, message_id: UUID | None = None) -> RobotHeartbeatEvent:
    return RobotHeartbeatEvent(
        message_id=message_id or uuid4(), robot_id=robot_id,
        occurred_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_heartbeat_refreshes_only_redis_with_backend_time_and_ttl() -> None:
    robot_id = uuid4()
    bot = robot(robot_id, status=RobotConnectionStatus.ONLINE)
    registry = FakeRegistry(bot)
    robot_status_realtime_publisher = FakeRealtimePublisher()
    bot_status_service = BotStatusService(registry, robot_status_realtime_publisher)
    message = heartbeat(robot_id)

    await bot_status_service.process_heartbeat_event(message)
    first = registry._redis_client.values[f"robot:{robot_id}:last_seen_at"]
    assert datetime.fromisoformat(first).year != 2020
    assert registry._redis_client.expirations[f"robot:{robot_id}:last_seen_at"] == 60
    await bot_status_service.process_heartbeat_event(message)
    assert datetime.fromisoformat(
        registry._redis_client.values[f"robot:{robot_id}:last_seen_at"]
    ) >= datetime.fromisoformat(first)
    assert set(registry._redis_client.values) == {f"robot:{robot_id}:last_seen_at"}
    assert registry._bot_repository.update_calls == 0
    assert bot.last_seen_at is None
    assert bot.last_sequence_number == 1
    assert robot_status_realtime_publisher.events == []


@pytest.mark.asyncio
async def test_unknown_offline_and_redis_failure_do_not_refresh_heartbeat() -> None:
    robot_id = uuid4()
    registry = FakeRegistry(robot(robot_id))
    bot_status_service = BotStatusService(registry, FakeRealtimePublisher())
    await bot_status_service.process_heartbeat_event(heartbeat(robot_id))
    assert registry._redis_client.values == {}

    registry._bot_repository.bot = None
    await bot_status_service.process_heartbeat_event(heartbeat(robot_id))
    assert registry._redis_client.values == {}

    registry._bot_repository.bot = robot(robot_id, status=RobotConnectionStatus.ONLINE)
    registry._redis_client.fail = True
    await bot_status_service.process_heartbeat_event(heartbeat(robot_id))
    assert registry._redis_client.values == {}
    assert registry._bot_repository.update_calls == 0


@pytest.mark.asyncio
async def test_online_heartbeat_offline_snapshots_last_heartbeat() -> None:
    robot_id = uuid4()
    map_id = uuid4()
    bot = robot(robot_id, map_id=map_id)
    registry = FakeRegistry(bot)
    robot_status_realtime_publisher = FakeRealtimePublisher()
    bot_status_service = BotStatusService(registry, robot_status_realtime_publisher)

    await bot_status_service.process_status_event(event(robot_id))
    assert bot.connection_status == RobotConnectionStatus.ONLINE
    assert bot.last_seen_at is not None
    await bot_status_service.process_heartbeat_event(heartbeat(robot_id))
    freshness = registry._redis_client.values[f"robot:{robot_id}:last_seen_at"]
    await bot_status_service.process_status_event(event(robot_id, 3, status=RobotConnectionStatus.OFFLINE))

    assert bot.last_seen_at == datetime.fromisoformat(freshness)
    assert f"robot:{robot_id}:last_seen_at" not in registry._redis_client.values
    assert registry._bot_repository.update_calls == 2
    assert len(robot_status_realtime_publisher.events) == 2
    assert all(
        published["group_id"] == bot.group_id and published["map_id"] == map_id
        for published in robot_status_realtime_publisher.events
    )
    await bot_status_service.process_heartbeat_event(heartbeat(robot_id))
    assert f"robot:{robot_id}:last_seen_at" not in registry._redis_client.values


@pytest.mark.asyncio
async def test_missing_freshness_preserves_offline_snapshot() -> None:
    robot_id = uuid4()
    bot = robot(robot_id, status=RobotConnectionStatus.ONLINE)
    bot.last_seen_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    registry = FakeRegistry(bot)
    await BotStatusService(registry, FakeRealtimePublisher()).process_status_event(
        event(robot_id, status=RobotConnectionStatus.OFFLINE)
    )
    assert bot.last_seen_at == datetime(2025, 1, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_offline_preserves_snapshot_when_redis_is_unavailable() -> None:
    robot_id = uuid4()
    bot = robot(robot_id, status=RobotConnectionStatus.ONLINE)
    bot.last_seen_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    redis = FakeRedis()
    redis.fail = True
    registry = FakeRegistry(bot, redis)
    await BotStatusService(registry, FakeRealtimePublisher()).process_status_event(
        event(robot_id, status=RobotConnectionStatus.OFFLINE)
    )
    assert bot.connection_status == RobotConnectionStatus.OFFLINE
    assert bot.last_seen_at == datetime(2025, 1, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_redis_unavailable", [False, True])
async def test_command_cancellation_failure_preserves_offline_status_and_fanout(status_redis_unavailable: bool) -> None:
    robot_id = uuid4()
    bot = robot(robot_id, status=RobotConnectionStatus.ONLINE, map_id=uuid4())
    redis = FakeRedis()
    redis.fail = status_redis_unavailable
    registry = FakeRegistry(bot, redis)
    robot_status_realtime_publisher = FakeRealtimePublisher()
    robot_action_service = SimpleNamespace(reconcile_robot=AsyncMock(side_effect=ConnectionError("Command store unavailable")))
    bot_status_service = BotStatusService(registry, robot_status_realtime_publisher, robot_action_service=robot_action_service)
    offline = event(robot_id, status=RobotConnectionStatus.OFFLINE)

    await bot_status_service.process_status_event(offline)

    robot_action_service.reconcile_robot.assert_awaited_once_with(robot_id)
    assert bot.connection_status == RobotConnectionStatus.OFFLINE
    assert bot.last_sequence_number == offline.sequence_number
    assert registry._bot_repository.update_calls == 1
    assert len(robot_status_realtime_publisher.events) == 1
    assert robot_status_realtime_publisher.events[0]["robot_id"] == robot_id
    assert robot_status_realtime_publisher.events[0]["connection_status"] == "OFFLINE"
    if not status_redis_unavailable:
        assert json.loads(redis.values[f"robot:{robot_id}:state"])["connection_status"] == "OFFLINE"

    # Broker redelivery is a duplicate and must not be needed to rescue fanout.
    await bot_status_service.process_status_event(offline)
    assert len(robot_status_realtime_publisher.events) == 1


@pytest.mark.asyncio
async def test_newer_same_status_persists_order_but_not_snapshot_or_broadcast() -> None:
    robot_id = uuid4()
    bot = robot(robot_id, status=RobotConnectionStatus.ONLINE)
    bot.last_seen_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    registry = FakeRegistry(bot)
    robot_status_realtime_publisher = FakeRealtimePublisher()
    bot_status_service = BotStatusService(registry, robot_status_realtime_publisher)

    newer = event(robot_id)
    await bot_status_service.process_status_event(newer)
    await bot_status_service.process_status_event(newer)
    await bot_status_service.process_status_event(event(robot_id, 1, status=RobotConnectionStatus.OFFLINE))
    assert registry._bot_repository.update_calls == 1
    assert bot.last_seen_at == datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert bot.last_sequence_number == 2
    assert robot_status_realtime_publisher.events == []


@pytest.mark.asyncio
async def test_operational_update_preserves_connectivity_timestamp() -> None:
    robot_id = uuid4()
    bot = robot(robot_id, status=RobotConnectionStatus.ONLINE)
    bot.last_seen_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    registry = FakeRegistry(bot)
    robot_status_realtime_publisher = FakeRealtimePublisher()
    await BotStatusService(registry, robot_status_realtime_publisher).process_status_event(
        event(robot_id, status=None, operational=RobotOperationalStatus.EXECUTING,
              kind="robot.status.changed")
    )
    assert bot.operational_status == RobotOperationalStatus.EXECUTING
    assert bot.last_seen_at == datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert robot_status_realtime_publisher.events == []


@pytest.mark.asyncio
async def test_ip_only_update_does_not_broadcast() -> None:
    robot_id = uuid4()
    bot = robot(robot_id, status=RobotConnectionStatus.ONLINE)
    bot.ip_address = "192.168.10.25"
    registry = FakeRegistry(bot)
    robot_status_realtime_publisher = FakeRealtimePublisher()

    await BotStatusService(registry, robot_status_realtime_publisher).process_status_event(event(robot_id))

    assert bot.ip_address == "192.168.10.24"
    assert bot.last_sequence_number == 2
    assert registry._bot_repository.update_calls == 1
    assert robot_status_realtime_publisher.events == []


@pytest.mark.asyncio
async def test_delivery_failure_does_not_undo_connectivity_change() -> None:
    robot_id = uuid4()
    bot = robot(robot_id, map_id=uuid4())
    registry = FakeRegistry(bot)
    robot_status_realtime_publisher = RobotStatusRealtimePublisher(FailingRoomService())  # type: ignore[arg-type]

    await BotStatusService(registry, robot_status_realtime_publisher).process_status_event(event(robot_id))

    assert bot.connection_status == RobotConnectionStatus.ONLINE
    assert bot.last_sequence_number == 2
    assert registry._bot_repository.update_calls == 1


@pytest.mark.asyncio
async def test_concurrent_offline_and_heartbeat_leave_no_live_freshness() -> None:
    robot_id = uuid4()
    bot = robot(robot_id, status=RobotConnectionStatus.ONLINE)
    registry = FakeRegistry(bot)
    bot_status_service = BotStatusService(registry, FakeRealtimePublisher())
    await asyncio.gather(
        bot_status_service.process_heartbeat_event(heartbeat(robot_id)),
        bot_status_service.process_status_event(event(robot_id, status=RobotConnectionStatus.OFFLINE)),
    )
    assert bot.connection_status == RobotConnectionStatus.OFFLINE
    assert f"robot:{robot_id}:last_seen_at" not in registry._redis_client.values


@pytest.mark.asyncio
async def test_delayed_lwt_cannot_overwrite_newer_reconnect() -> None:
    robot_id = uuid4()
    bot = robot(robot_id, status=RobotConnectionStatus.ONLINE)
    registry = FakeRegistry(bot)
    robot_status_realtime_publisher = FakeRealtimePublisher()
    bot_status_service = BotStatusService(registry, robot_status_realtime_publisher)
    await bot_status_service.process_status_event(event(robot_id, 2, status=RobotConnectionStatus.OFFLINE))
    await bot_status_service.process_status_event(event(robot_id, 4, status=RobotConnectionStatus.ONLINE))
    await bot_status_service.process_status_event(event(robot_id, 3, status=RobotConnectionStatus.OFFLINE))

    assert bot.connection_status == RobotConnectionStatus.ONLINE
    assert bot.last_sequence_number == 4
    assert len(robot_status_realtime_publisher.events) == 2


@pytest.mark.asyncio
async def test_operational_event_cannot_overtake_queued_online_event() -> None:
    robot_id = uuid4()
    bot = robot(robot_id)
    registry = FakeRegistry(bot)
    bot_status_service = BotStatusService(registry, FakeRealtimePublisher())
    await bot_status_service.process_status_event(
        event(robot_id, 3, status=None, operational=RobotOperationalStatus.EXECUTING,
              kind="robot.status.changed")
    )
    await bot_status_service.process_status_event(event(robot_id, 2))
    assert bot.connection_status == RobotConnectionStatus.ONLINE
    assert bot.last_sequence_number == 2
    assert bot.operational_status == RobotOperationalStatus.IDLE
