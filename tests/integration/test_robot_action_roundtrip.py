"""Opt-in tests against disposable brokers and Redis, using the actual simulator.

Run with scripts/test_robot_actions.py --integration. Database lookups are
substituted with a tenant-scoped context; all messaging and Redis Lua are real.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TypeVar

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import aio_pika
import paho.mqtt.client as mqtt
from pydantic import ValidationError
import pytest
import pytest_asyncio
from redis.asyncio import Redis

from app.common.context import AppContext
from app.common.enum.context_actions import SEND_ROBOT_COMMAND
from app.common.enum.robot import RobotConnectionStatus, RobotOperationalStatus
from app.common.enum.user_roles import GroupRole
from app.common.schemas.robot_action import (
    JsonValue, RobotAcknowledgement, RobotCommand, RobotCommandRecord, RobotCommandRequest,
)
from app.core.config import settings
from app.external.mqtt.robot_action import RobotActionMqttGateway
from app.external.queues.queue import RabbitMQClient
from app.external.queues.topics.robot_action import RobotActionTopic
from app.external.redis.redis import RedisClient
from app.external.redis.robot_commands import RobotCommandStore
from app.services.robot_action import RobotActionService


T = TypeVar("T")


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get("PLEACO_ROBOT_ACTION_INTEGRATION") != "1",
    reason="Requires explicit --integration and disposable services",
)]
SIMULATOR = Path(__file__).resolve().parents[3] / "pleaco-mock-device"
AMQP_URL = "amqp://guest:guest@127.0.0.1:5673/"


class MqttProbe:
    def __init__(self, robot_id: UUID) -> None:
        self.robot_id = robot_id
        self.messages = []
        self.ready = threading.Event()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"test-probe-{uuid4()}")
        self.client.on_connect = self._connect
        self.client.on_subscribe = lambda *_: self.ready.set()
        self.client.on_message = self._message

    def _connect(self, client: mqtt.Client, *_: object) -> None:
        client.subscribe(f"pleco/robots/{self.robot_id}/#", qos=1)

    def _message(self, _client: mqtt.Client, _userdata: object, message: mqtt.MQTTMessage) -> None:
        self.messages.append((message.topic, json.loads(message.payload), time.monotonic()))

    def start(self) -> None:
        self.client.connect("127.0.0.1", 1884, keepalive=10)
        self.client.loop_start()
        assert self.ready.wait(5), "Test MQTT probe did not subscribe"

    def stop(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()

    def events(self, suffix: str) -> list[tuple[str, dict[str, JsonValue], float]]:
        return [item for item in self.messages if item[0].endswith(f"/{suffix}")]


async def eventually(predicate: Callable[[], T], timeout: float = 8) -> T:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        result = predicate()
        if result:
            return result
        await asyncio.sleep(0.02)
    raise AssertionError("Expected asynchronous event did not arrive")


async def command_status(harness: SimpleNamespace, command_id: UUID | str, expected: str, timeout: float = 8) -> RobotCommandRecord:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = await harness.robot_command_store.get(harness.robot_id, command_id)
        if record and record["status"] == expected:
            return record
        await asyncio.sleep(0.02)
    raise AssertionError(f"Command did not reach {expected}")


@pytest_asyncio.fixture
async def harness(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SimpleNamespace]:
    for name, value in {
        "RABBITMQ_URL": AMQP_URL, "REDIS_HOST": "127.0.0.1", "REDIS_PORT": 6380,
        "REDIS_PASSWORD": "", "MQTT_HOST": "127.0.0.1", "MQTT_PORT": 1884,
        "MQTT_USERNAME": "", "MQTT_PASSWORD": "",
    }.items():
        monkeypatch.setattr(settings, name, value)
    robot_id, group_id, map_id, actor_id = (uuid4() for _ in range(4))
    context = {
        "robot_id": robot_id, "group_id": group_id, "map_id": map_id,
        "connection_status": RobotConnectionStatus.ONLINE,
        "operational_status": RobotOperationalStatus.IDLE,
        "dimension_x": Decimal("20"), "dimension_y": Decimal("20"),
        "dock_x": Decimal("5"), "dock_y": Decimal("5"),
    }
    repository = SimpleNamespace(
        get_context=AsyncMock(side_effect=lambda *_: context),
        list_robot_ids=AsyncMock(return_value=[robot_id]),
    )

    async def transaction(callback: Callable[[None], Awaitable[T]]) -> T:
        return await callback(None)

    registry = SimpleNamespace(robot_action_repo=lambda: repository, transaction_wrapper=transaction)
    permission_service = SimpleNamespace(
        get_group_member=AsyncMock(return_value=SimpleNamespace(role=GroupRole.MEMBER)),
        is_action_executable=lambda **_: True,
    )
    redis_client = RedisClient()
    raw_redis = Redis(host="127.0.0.1", port=6380, decode_responses=True)
    robot_command_store = RobotCommandStore(redis_client)
    rabbitmq_client = RabbitMQClient(url=AMQP_URL)
    robot_action_topic = RobotActionTopic(rabbitmq_client)
    robot_action_mqtt_gateway = RobotActionMqttGateway(robot_action_topic, robot_command_store)
    robot_action_service = RobotActionService(registry, permission_service, robot_command_store, robot_action_topic, robot_action_mqtt_gateway)
    mqtt_probe = MqttProbe(robot_id)
    auth = {
        "robot_id": robot_id, "group_id": group_id,
        "credential": SimpleNamespace(id=actor_id, active_group_id=group_id),
        "ctx": AppContext(trace_id=uuid4(), action=SEND_ROBOT_COMMAND, actor=actor_id),
    }
    result = SimpleNamespace(
        robot_id=robot_id, group_id=group_id, map_id=map_id, context=context,
        robot_action_service=robot_action_service, robot_command_store=robot_command_store, raw_redis=raw_redis, robot_action_mqtt_gateway=robot_action_mqtt_gateway,
        robot_action_topic=robot_action_topic, auth=auth, mqtt_probe=mqtt_probe,
        registry=registry, permission_service=permission_service, rabbitmq_client=rabbitmq_client,
    )
    try:
        await raw_redis.ping()
        await redis_client.set(f"robot:{robot_id}:last_seen_at", datetime.now(timezone.utc).isoformat(), expire=60)
        await robot_action_service.start()
        await robot_action_mqtt_gateway.start()
        await eventually(robot_action_mqtt_gateway._client.is_connected)
        await asyncio.to_thread(mqtt_probe.start)
        yield result
    finally:
        await robot_action_mqtt_gateway.stop()
        await asyncio.to_thread(mqtt_probe.stop)
        await rabbitmq_client.close()
        connection = await aio_pika.connect_robust(AMQP_URL)
        try:
            channel = await connection.channel()
            name = f"pleaco-backend.robot-actions.{robot_id}"
            for suffix in ("", ".retry", ".dlq"):
                queue = await channel.get_queue(name + suffix, ensure=False)
                await queue.delete(if_unused=False, if_empty=False)
            await channel.exchange_delete(name + ".dlx", if_unused=False)
        finally:
            await connection.close()
        keys = [key async for key in raw_redis.scan_iter(match=f"robot:{robot_id}:*")]
        if keys:
            await raw_redis.delete(*keys)
        await raw_redis.aclose()
        await redis_client.pool.disconnect()


@asynccontextmanager
async def simulator(harness: SimpleNamespace, tmp_path: Path, *, force_failure: bool = False, delay: float = 20) -> AsyncIterator[subprocess.Popen[bytes]]:
    # Deliberately construct a fresh environment, without inheriting app secrets.
    process = subprocess.Popen(
        [sys.executable, "-m", "app.main"], cwd=SIMULATOR,
        env={
            "ROBOT_ID": str(harness.robot_id), "ROBOT_SERIAL_NUM": "integration-only",
            "ROBOT_IP_ADDRESS": "127.0.0.1", "MQTT_HOST": "127.0.0.1", "MQTT_PORT": "1884",
            "ROBOT_STATE_PATH": str(tmp_path / "sequence.txt"),
            "ROBOT_COMMAND_STATE_PATH": str(tmp_path / "commands.json"),
            "ROBOT_COMMAND_FORCE_FAILURE": str(force_failure).lower(),
            "ROBOT_COMMAND_ACK_DELAY_SECONDS": str(delay),
        }, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        await eventually(lambda: any(
            payload.get("event") == "robot.online" for _, payload, _ in harness.mqtt_probe.events("status")
        ))
        assert process.poll() is None
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=5)


def request(direction: str = "NORTH", distance: str = "1m") -> RobotCommandRequest:
    return RobotCommandRequest(action={
        "is_start": True, "is_finish": False, "direction": direction, "distance": distance,
    })


@pytest.mark.asyncio
async def test_real_twenty_second_roundtrip_and_duplicate_ack(harness: SimpleNamespace, tmp_path: Path) -> None:
    async with simulator(harness, tmp_path):
        command = await harness.robot_action_service.submit(request=request(), **harness.auth)
        await command_status(harness, command.command_id, "AWAITING_ACK")
        assert await harness.robot_command_store.position(harness.robot_id) is None
        await command_status(harness, command.command_id, "SUCCEEDED", timeout=27)
        position = await harness.robot_command_store.position(harness.robot_id)
        assert (position["x"], position["y"]) == (5, 6)
        assert position["group_id"] == str(harness.group_id)
        assert 43190 <= await harness.raw_redis.ttl(harness.robot_command_store.position_key(harness.robot_id)) <= 43200
        commands, acks = harness.mqtt_probe.events("commands"), harness.mqtt_probe.events("acks")
        assert len(commands) == 1
        assert acks[0][2] - commands[0][2] >= 19.5
        assert any(payload.get("event") == "robot.heartbeat" for _, payload, _ in harness.mqtt_probe.events("status"))
        # A repeated success must neither move again nor refresh the position TTL.
        await harness.raw_redis.expire(harness.robot_command_store.position_key(harness.robot_id), 120)
        info = harness.mqtt_probe.client.publish(
            f"pleco/robots/{harness.robot_id}/acks", json.dumps(acks[0][1]), qos=1,
        )
        await asyncio.to_thread(info.wait_for_publish, timeout=3)
        await asyncio.sleep(0.15)
        assert await harness.robot_command_store.position(harness.robot_id) == position
        assert await harness.raw_redis.ttl(harness.robot_command_store.position_key(harness.robot_id)) <= 120


@pytest.mark.asyncio
async def test_failed_robot_actions_retry_exactly_three_times(harness: SimpleNamespace, tmp_path: Path) -> None:
    async with simulator(harness, tmp_path, force_failure=True, delay=0.05):
        command = await harness.robot_action_service.submit(request=request("EAST", "0.5m"), **harness.auth)
        await command_status(harness, command.command_id, "FAILED")
        await asyncio.sleep(0.15)
        commands = harness.mqtt_probe.events("commands")
        assert [payload["attempt"] for _, payload, _ in commands] == [1, 2, 3, 4]
        assert {payload["command_id"] for _, payload, _ in commands} == {str(command.command_id)}
        assert await harness.robot_command_store.position(harness.robot_id) is None
        assert await harness.robot_command_store.active(harness.robot_id) is None


@pytest.mark.asyncio
async def test_disconnect_cancels_work_and_preserves_confirmed_position(harness: SimpleNamespace, tmp_path: Path) -> None:
    async with simulator(harness, tmp_path, delay=0.05):
        first = await harness.robot_action_service.submit(request=request(), **harness.auth)
        await command_status(harness, first.command_id, "SUCCEEDED")
        position = await harness.robot_command_store.position(harness.robot_id)
        await harness.raw_redis.expire(harness.robot_command_store.position_key(harness.robot_id), 120)
    # A second send reaches MQTT but there is no robot to ACK it.
    second = await harness.robot_action_service.submit(request=request(), **harness.auth)
    await command_status(harness, second.command_id, "AWAITING_ACK")
    harness.context["connection_status"] = RobotConnectionStatus.OFFLINE
    await harness.robot_action_service.reconcile_robot(harness.robot_id)
    await command_status(harness, second.command_id, "CANCELLED")
    late_ack = RobotAcknowledgement(**{
        key: second.model_dump(mode="json")[key] for key in RobotCommand.model_fields
    }, ack=True)
    await harness.robot_action_mqtt_gateway.handle_message(f"pleco/robots/{harness.robot_id}/acks", late_ack.model_dump_json().encode())
    assert await harness.robot_command_store.position(harness.robot_id) == position
    assert await harness.raw_redis.ttl(harness.robot_command_store.position_key(harness.robot_id)) <= 120


@pytest.mark.asyncio
async def test_atomic_success_racing_duplicate_delivery(harness: SimpleNamespace) -> None:
    command = await harness.robot_action_service.submit(request=request(), **harness.auth)
    await command_status(harness, command.command_id, "AWAITING_ACK")
    ack = RobotAcknowledgement(**{
        key: command.model_dump(mode="json")[key] for key in RobotCommand.model_fields
    }, ack=True)
    await asyncio.gather(*(harness.robot_action_service.process_message(ack) for _ in range(20)))
    await command_status(harness, command.command_id, "SUCCEEDED")
    position = await harness.robot_command_store.position(harness.robot_id)
    assert (position["x"], position["y"]) == (5, 6)
    assert await harness.robot_command_store.active(harness.robot_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("unconfirmed", [False, True])
async def test_backend_restart_recovers_only_unconfirmed_dispatch(harness: SimpleNamespace, unconfirmed: bool) -> None:
    command = await harness.robot_action_service.submit(request=request(), **harness.auth)
    record = await command_status(harness, command.command_id, "AWAITING_ACK")
    await eventually(lambda: harness.mqtt_probe.events("commands"))
    # Wait for the original publisher confirm before modelling the crash window.
    for _ in range(100):
        if await harness.robot_command_store.dispatched(record):
            break
        await asyncio.sleep(0.02)
    assert await harness.robot_command_store.dispatched(record)
    if unconfirmed:
        await harness.raw_redis.delete(harness.robot_command_store.dispatch_key(record))
    await harness.robot_action_mqtt_gateway.stop()
    await harness.rabbitmq_client.close()
    rabbitmq_client = RabbitMQClient(url=AMQP_URL)
    robot_action_topic = RobotActionTopic(rabbitmq_client)
    robot_action_mqtt_gateway = RobotActionMqttGateway(robot_action_topic, harness.robot_command_store)
    robot_action_service = RobotActionService(harness.registry, harness.permission_service, harness.robot_command_store, robot_action_topic, robot_action_mqtt_gateway)
    try:
        await robot_action_mqtt_gateway.start()
        await eventually(robot_action_mqtt_gateway._client.is_connected)
        await robot_action_service.start()
        await robot_action_service.reconcile_robot(harness.robot_id)
        if unconfirmed:
            await eventually(lambda: len(harness.mqtt_probe.events("commands")) == 2)
        else:
            await asyncio.sleep(0.15)
            assert len(harness.mqtt_probe.events("commands")) == 1
        # All recovery publications retain the logical command and attempt.
        assert all(payload["command_id"] == str(command.command_id) and payload["attempt"] == 1
                   for _, payload, _ in harness.mqtt_probe.events("commands"))
        ack = RobotAcknowledgement(**{
            key: command.model_dump(mode="json")[key] for key in RobotCommand.model_fields
        }, ack=True)
        info = harness.mqtt_probe.client.publish(
            f"pleco/robots/{harness.robot_id}/acks", ack.model_dump_json(), qos=1,
        )
        await asyncio.to_thread(info.wait_for_publish, timeout=3)
        await command_status(harness, command.command_id, "SUCCEEDED")
        assert (await harness.robot_command_store.position(harness.robot_id))["y"] == 6
    finally:
        await robot_action_mqtt_gateway.stop()
        await rabbitmq_client.close()


@pytest.mark.asyncio
async def test_timeout_never_resends_and_allows_fresh_trigger(harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ROBOT_COMMAND_TIMEOUT_SECONDS", 1)
    command = await harness.robot_action_service.submit(request=request(), **harness.auth)
    await command_status(harness, command.command_id, "AWAITING_ACK")
    await asyncio.sleep(1.1)
    await harness.robot_action_service.reconcile_robot(harness.robot_id)
    await command_status(harness, command.command_id, "TIMED_OUT")
    assert len(harness.mqtt_probe.events("commands")) == 1
    assert await harness.robot_command_store.position(harness.robot_id) is None
    fresh = await harness.robot_action_service.submit(request=request(), **harness.auth)
    assert fresh.command_id != command.command_id
    await command_status(harness, fresh.command_id, "AWAITING_ACK")
    ack = RobotAcknowledgement(**{
        key: command.model_dump(mode="json")[key] for key in RobotCommand.model_fields
    }, ack=True)
    await harness.robot_action_topic.publish_ack(ack)
    await asyncio.sleep(0.1)
    assert await harness.robot_command_store.position(harness.robot_id) is None
    assert (await harness.robot_command_store.active(harness.robot_id))["command_id"] == str(fresh.command_id)


@pytest.mark.asyncio
async def test_evicted_command_record_releases_only_its_dangling_reservation(harness: SimpleNamespace) -> None:
    command = await harness.robot_action_service.submit(request=request(), **harness.auth)
    await command_status(harness, command.command_id, "AWAITING_ACK")
    await harness.raw_redis.delete(harness.robot_command_store.command_key(harness.robot_id, command.command_id))
    assert await harness.robot_command_store.active(harness.robot_id) is None
    assert await harness.raw_redis.get(harness.robot_command_store.active_key(harness.robot_id)) is None
    fresh = await harness.robot_action_service.submit(request=request(), **harness.auth)
    assert fresh.command_id != command.command_id


def test_backend_and_simulator_accept_identical_command_contracts() -> None:
    spec = importlib.util.spec_from_file_location("integration_simulator_models", SIMULATOR / "app/models.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    base = {"robot_id": str(uuid4()), "command_id": str(uuid4()), "attempt": 1,
            "action": {"is_start": True, "is_finish": False, "direction": "NORTH"}}
    for value in ("1m", "0.5m", "1.25m", "0m", "-1m", "01m", "1km", "NaNm", "1" * 64 + "m"):
        payload = {**base, "action": {**base["action"], "distance": value}}
        outcomes = []
        for model in (RobotCommand, module.RobotCommand):
            try:
                outcomes.append(model.model_validate(payload).model_dump(mode="json"))
            except ValidationError:
                outcomes.append(None)
        assert outcomes[0] == outcomes[1], value
