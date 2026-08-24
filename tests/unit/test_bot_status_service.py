import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.common.enum.robot import RobotConnectionStatus, RobotOperationalStatus
from app.common.schemas.robot_status import RobotStatusEvent
from app.services.bot_status import BotStatusService


class FakeRedis:
    def __init__(self, state=None):
        self.values = {} if state is None else {"robot:state": json.dumps(state)}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value):
        self.values[key] = value


class FakeBotRepository:
    def __init__(self, bot):
        self.bot = bot
        self.update_calls = 0

    async def get_by_id(self, _session, _bot_id, ctx):
        return self.bot

    async def update_status(self, _session, bot, **kwargs):
        self.update_calls += 1
        bot.ip_address = kwargs["ip_address"]
        bot.connection_status = kwargs["connection_status"]
        bot.operational_status = kwargs["operational_status"]
        bot.last_seen_at = kwargs["last_seen_at"]
        bot.last_sequence_number = kwargs["last_sequence_number"]
        bot.last_message_id = kwargs["last_message_id"]
        return bot


class FakeRegistry:
    def __init__(self, bot, redis):
        self._redis_client = redis
        self._bot_repository = FakeBotRepository(bot)

    def bot_repo(self):
        return self._bot_repository

    async def transaction_wrapper(self, callback):
        return await callback(object())


class FakeWebSocketManager:
    def __init__(self):
        self.events = []

    async def broadcast(self, group_id, payload):
        self.events.append((group_id, payload))


def event(robot_id, sequence=2, *, status=RobotConnectionStatus.ONLINE, ip="192.168.10.24"):
    return RobotStatusEvent(
        message_id=uuid4(),
        type="robot.connection.changed",
        robot_id=robot_id,
        ip_address=ip,
        sequence_number=sequence,
        connection_status=status,
        occurred_at=datetime.now(timezone.utc),
    )


def robot(robot_id):
    return SimpleNamespace(
        id=robot_id,
        group_id=uuid4(),
        ip_address="192.168.10.24",
        connection_status=RobotConnectionStatus.OFFLINE,
        operational_status=RobotOperationalStatus.IDLE,
        last_seen_at=None,
        last_sequence_number=1,
        last_message_id=uuid4(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cache", "incoming_status", "expected_writes", "expected_broadcasts"),
    [
        ({"last_sequence_number": 1, "last_message_id": "other", "ip_address": "192.168.10.24", "connection_status": "OFFLINE", "operational_status": "IDLE"}, RobotConnectionStatus.OFFLINE, 0, 0),
        ({"last_sequence_number": 1, "last_message_id": "other", "ip_address": "192.168.10.24", "connection_status": "OFFLINE", "operational_status": "IDLE"}, RobotConnectionStatus.ONLINE, 1, 1),
        (None, RobotConnectionStatus.OFFLINE, 0, 0),
        (None, RobotConnectionStatus.ONLINE, 1, 1),
    ],
    ids=["redis-hit-same", "redis-hit-different", "redis-miss-db-same", "redis-miss-db-different"],
)
async def test_reconciles_cache_and_database_state(
    cache, incoming_status, expected_writes, expected_broadcasts
):
    robot_id = uuid4()
    state = robot(robot_id)
    redis = FakeRedis()
    if cache is not None:
        redis.values[f"robot:{robot_id}:state"] = json.dumps(cache)
    registry = FakeRegistry(state, redis)
    sockets = FakeWebSocketManager()

    await BotStatusService(registry, sockets).process_status_event(
        event(robot_id, status=incoming_status)
    )

    assert registry._bot_repository.update_calls == expected_writes
    assert len(sockets.events) == expected_broadcasts
    assert json.loads(redis.values[f"robot:{robot_id}:state"])["last_sequence_number"] == 2


@pytest.mark.asyncio
async def test_rejects_unknown_duplicate_and_stale_events():
    robot_id = uuid4()
    sockets = FakeWebSocketManager()
    unknown_registry = FakeRegistry(None, FakeRedis())
    await BotStatusService(unknown_registry, sockets).process_status_event(event(robot_id))
    assert unknown_registry._bot_repository.update_calls == 0

    state = robot(robot_id)
    duplicate = event(robot_id)
    cache = {
        "last_sequence_number": 2,
        "last_message_id": str(duplicate.message_id),
        "ip_address": "192.168.10.24",
        "connection_status": "OFFLINE",
        "operational_status": "IDLE",
    }
    registry = FakeRegistry(state, FakeRedis())
    registry._redis_client.values[f"robot:{robot_id}:state"] = json.dumps(cache)
    service = BotStatusService(registry, sockets)
    await service.process_status_event(duplicate)
    await service.process_status_event(event(robot_id, sequence=1))
    assert registry._bot_repository.update_calls == 0


@pytest.mark.asyncio
async def test_updates_ip_metadata_without_changing_robot_identity():
    robot_id = uuid4()
    state = robot(robot_id)
    registry = FakeRegistry(state, FakeRedis())
    sockets = FakeWebSocketManager()

    await BotStatusService(registry, sockets).process_status_event(
        event(robot_id, status=RobotConnectionStatus.OFFLINE, ip="192.168.10.31")
    )

    assert registry._bot_repository.update_calls == 1
    assert state.id == robot_id
    assert state.ip_address == "192.168.10.31"
