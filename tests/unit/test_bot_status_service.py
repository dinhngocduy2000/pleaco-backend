import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.common.enum.robot import RobotConnectionStatus, RobotOperationalStatus
from app.common.schemas.robot_status import RobotHeartbeatEvent, RobotStatusEvent
from app.external.realtime.robot_status import RobotStatusRealtimePublisher
from app.external.realtime.rooms import RoomOperationError
from app.services.bot_status import BotStatusService


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.expirations = {}
        self.fail = False

    async def get(self, key):
        if self.fail:
            raise ConnectionError("Redis unavailable")
        return self.values.get(key)

    async def set(self, key, value, expire=None):
        if self.fail:
            raise ConnectionError("Redis unavailable")
        self.values[key] = value
        self.expirations[key] = expire

    async def update_robot_status_cache(
        self, robot_id, sequence, state, connection_status,
        connection_changed, received_at, ttl,
    ):
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
    def __init__(self, bot):
        self.bot = bot
        self.update_calls = 0

    async def get_by_id_for_update(self, _session, _bot_id):
        return self.bot

    async def update_status(self, _session, bot, **kwargs):
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
    def __init__(self, bot, redis=None):
        self._redis_client = redis or FakeRedis()
        self._bot_repository = FakeBotRepository(bot)
        self.lock = asyncio.Lock()

    def bot_repo(self):
        return self._bot_repository

    async def transaction_wrapper(self, callback):
        async with self.lock:
            return await callback(object())


class FakeRealtimePublisher:
    def __init__(self):
        self.events = []

    async def publish(self, **kwargs):
        self.events.append(kwargs)


class FailingRoomService:
    async def emit(self, _event, _payload, _rooms):
        raise RoomOperationError("Socket.IO unavailable")


def event(robot_id, sequence=2, *, status=RobotConnectionStatus.ONLINE,
          operational=None, kind="robot.connection.changed"):
    return RobotStatusEvent(
        message_id=uuid4(), type=kind, robot_id=robot_id,
        ip_address="192.168.10.24", sequence_number=sequence,
        connection_status=status, operational_status=operational,
        occurred_at=datetime.now(timezone.utc),
    )


def robot(robot_id, *, status=RobotConnectionStatus.OFFLINE, map_id=None):
    return SimpleNamespace(
        id=robot_id, group_id=uuid4(), map_id=map_id, ip_address="192.168.10.24",
        connection_status=status, operational_status=RobotOperationalStatus.IDLE,
        last_seen_at=None, last_sequence_number=1, last_message_id=uuid4(),
    )


def heartbeat(robot_id, message_id=None):
    return RobotHeartbeatEvent(
        message_id=message_id or uuid4(), robot_id=robot_id,
        occurred_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_heartbeat_refreshes_only_redis_with_backend_time_and_ttl():
    robot_id = uuid4()
    bot = robot(robot_id, status=RobotConnectionStatus.ONLINE)
    registry = FakeRegistry(bot)
    publisher = FakeRealtimePublisher()
    service = BotStatusService(registry, publisher)
    message = heartbeat(robot_id)

    await service.process_heartbeat_event(message)
    first = registry._redis_client.values[f"robot:{robot_id}:last_seen_at"]
    assert datetime.fromisoformat(first).year != 2020
    assert registry._redis_client.expirations[f"robot:{robot_id}:last_seen_at"] == 60
    await service.process_heartbeat_event(message)
    assert datetime.fromisoformat(
        registry._redis_client.values[f"robot:{robot_id}:last_seen_at"]
    ) >= datetime.fromisoformat(first)
    assert set(registry._redis_client.values) == {f"robot:{robot_id}:last_seen_at"}
    assert registry._bot_repository.update_calls == 0
    assert bot.last_seen_at is None
    assert bot.last_sequence_number == 1
    assert publisher.events == []


@pytest.mark.asyncio
async def test_unknown_offline_and_redis_failure_do_not_refresh_heartbeat():
    robot_id = uuid4()
    registry = FakeRegistry(robot(robot_id))
    service = BotStatusService(registry, FakeRealtimePublisher())
    await service.process_heartbeat_event(heartbeat(robot_id))
    assert registry._redis_client.values == {}

    registry._bot_repository.bot = None
    await service.process_heartbeat_event(heartbeat(robot_id))
    assert registry._redis_client.values == {}

    registry._bot_repository.bot = robot(robot_id, status=RobotConnectionStatus.ONLINE)
    registry._redis_client.fail = True
    await service.process_heartbeat_event(heartbeat(robot_id))
    assert registry._redis_client.values == {}
    assert registry._bot_repository.update_calls == 0


@pytest.mark.asyncio
async def test_online_heartbeat_offline_snapshots_last_heartbeat():
    robot_id = uuid4()
    map_id = uuid4()
    bot = robot(robot_id, map_id=map_id)
    registry = FakeRegistry(bot)
    publisher = FakeRealtimePublisher()
    service = BotStatusService(registry, publisher)

    await service.process_status_event(event(robot_id))
    assert bot.connection_status == RobotConnectionStatus.ONLINE
    assert bot.last_seen_at is not None
    await service.process_heartbeat_event(heartbeat(robot_id))
    freshness = registry._redis_client.values[f"robot:{robot_id}:last_seen_at"]
    await service.process_status_event(event(robot_id, 3, status=RobotConnectionStatus.OFFLINE))

    assert bot.last_seen_at == datetime.fromisoformat(freshness)
    assert f"robot:{robot_id}:last_seen_at" not in registry._redis_client.values
    assert registry._bot_repository.update_calls == 2
    assert len(publisher.events) == 2
    assert all(
        published["group_id"] == bot.group_id and published["map_id"] == map_id
        for published in publisher.events
    )
    await service.process_heartbeat_event(heartbeat(robot_id))
    assert f"robot:{robot_id}:last_seen_at" not in registry._redis_client.values


@pytest.mark.asyncio
async def test_missing_freshness_preserves_offline_snapshot():
    robot_id = uuid4()
    bot = robot(robot_id, status=RobotConnectionStatus.ONLINE)
    bot.last_seen_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    registry = FakeRegistry(bot)
    await BotStatusService(registry, FakeRealtimePublisher()).process_status_event(
        event(robot_id, status=RobotConnectionStatus.OFFLINE)
    )
    assert bot.last_seen_at == datetime(2025, 1, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_offline_preserves_snapshot_when_redis_is_unavailable():
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
async def test_newer_same_status_persists_order_but_not_snapshot_or_broadcast():
    robot_id = uuid4()
    bot = robot(robot_id, status=RobotConnectionStatus.ONLINE)
    bot.last_seen_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    registry = FakeRegistry(bot)
    publisher = FakeRealtimePublisher()
    service = BotStatusService(registry, publisher)

    newer = event(robot_id)
    await service.process_status_event(newer)
    await service.process_status_event(newer)
    await service.process_status_event(event(robot_id, 1, status=RobotConnectionStatus.OFFLINE))
    assert registry._bot_repository.update_calls == 1
    assert bot.last_seen_at == datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert bot.last_sequence_number == 2
    assert publisher.events == []


@pytest.mark.asyncio
async def test_operational_update_preserves_connectivity_timestamp():
    robot_id = uuid4()
    bot = robot(robot_id, status=RobotConnectionStatus.ONLINE)
    bot.last_seen_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    registry = FakeRegistry(bot)
    publisher = FakeRealtimePublisher()
    await BotStatusService(registry, publisher).process_status_event(
        event(robot_id, status=None, operational=RobotOperationalStatus.EXECUTING,
              kind="robot.status.changed")
    )
    assert bot.operational_status == RobotOperationalStatus.EXECUTING
    assert bot.last_seen_at == datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert publisher.events == []


@pytest.mark.asyncio
async def test_ip_only_update_does_not_broadcast():
    robot_id = uuid4()
    bot = robot(robot_id, status=RobotConnectionStatus.ONLINE)
    bot.ip_address = "192.168.10.25"
    registry = FakeRegistry(bot)
    publisher = FakeRealtimePublisher()

    await BotStatusService(registry, publisher).process_status_event(event(robot_id))

    assert bot.ip_address == "192.168.10.24"
    assert bot.last_sequence_number == 2
    assert registry._bot_repository.update_calls == 1
    assert publisher.events == []


@pytest.mark.asyncio
async def test_delivery_failure_does_not_undo_connectivity_change():
    robot_id = uuid4()
    bot = robot(robot_id, map_id=uuid4())
    registry = FakeRegistry(bot)
    publisher = RobotStatusRealtimePublisher(FailingRoomService())  # type: ignore[arg-type]

    await BotStatusService(registry, publisher).process_status_event(event(robot_id))

    assert bot.connection_status == RobotConnectionStatus.ONLINE
    assert bot.last_sequence_number == 2
    assert registry._bot_repository.update_calls == 1


@pytest.mark.asyncio
async def test_concurrent_offline_and_heartbeat_leave_no_live_freshness():
    robot_id = uuid4()
    bot = robot(robot_id, status=RobotConnectionStatus.ONLINE)
    registry = FakeRegistry(bot)
    service = BotStatusService(registry, FakeRealtimePublisher())
    await asyncio.gather(
        service.process_heartbeat_event(heartbeat(robot_id)),
        service.process_status_event(event(robot_id, status=RobotConnectionStatus.OFFLINE)),
    )
    assert bot.connection_status == RobotConnectionStatus.OFFLINE
    assert f"robot:{robot_id}:last_seen_at" not in registry._redis_client.values


@pytest.mark.asyncio
async def test_delayed_lwt_cannot_overwrite_newer_reconnect():
    robot_id = uuid4()
    bot = robot(robot_id, status=RobotConnectionStatus.ONLINE)
    registry = FakeRegistry(bot)
    publisher = FakeRealtimePublisher()
    service = BotStatusService(registry, publisher)
    await service.process_status_event(event(robot_id, 2, status=RobotConnectionStatus.OFFLINE))
    await service.process_status_event(event(robot_id, 4, status=RobotConnectionStatus.ONLINE))
    await service.process_status_event(event(robot_id, 3, status=RobotConnectionStatus.OFFLINE))

    assert bot.connection_status == RobotConnectionStatus.ONLINE
    assert bot.last_sequence_number == 4
    assert len(publisher.events) == 2


@pytest.mark.asyncio
async def test_operational_event_cannot_overtake_queued_online_event():
    robot_id = uuid4()
    bot = robot(robot_id)
    registry = FakeRegistry(bot)
    service = BotStatusService(registry, FakeRealtimePublisher())
    await service.process_status_event(
        event(robot_id, 3, status=None, operational=RobotOperationalStatus.EXECUTING,
              kind="robot.status.changed")
    )
    await service.process_status_event(event(robot_id, 2))
    assert bot.connection_status == RobotConnectionStatus.ONLINE
    assert bot.last_sequence_number == 2
    assert bot.operational_status == RobotOperationalStatus.IDLE
