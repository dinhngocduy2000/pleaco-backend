"""Paho dispatch lifetime and actual application startup regressions."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import NoReturn, TypeVar

import asyncio
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import paho.mqtt.client as mqtt
import pytest

from app.common.schemas.robot_action import CleaningAction, RobotCommand
from app.core.config import settings
from app.external.mqtt.robot_action import RobotActionMqttGateway
from app.repository.registry import Registry


T = TypeVar("T")


def command() -> RobotCommand:
    return RobotCommand(robot_id=uuid4(), command_id=uuid4(), attempt=1,
                        action=CleaningAction(is_start=True, is_finish=False, direction="NORTH"))


def connected_publisher(monkeypatch: pytest.MonkeyPatch, robot_action_mqtt_gateway: RobotActionMqttGateway, *, block_first_send: bool = False) -> tuple[mqtt.Client, socket.socket, socket.socket]:
    """Keep real Paho publication/packet queues; replace only broker connection."""
    client = robot_action_mqtt_gateway._new_publisher()
    local, peer = socket.socketpair()
    local.setblocking(False)
    def connect(*args: object, **kwargs: object) -> mqtt.MQTTErrorCode:
        client._sock = local
        client._state = mqtt._ConnectionState.MQTT_CS_CONNECTED
        return mqtt.MQTT_ERR_SUCCESS
    monkeypatch.setattr(client, "connect", connect)
    monkeypatch.setattr(client, "loop", lambda timeout: (time.sleep(min(timeout, 0.001)) or mqtt.MQTT_ERR_SUCCESS))
    if block_first_send:
        original = client._sock_send
        calls = []
        def send(data: bytes | bytearray | memoryview) -> int:
            calls.append(bytes(data))
            if len(calls) == 1:
                raise BlockingIOError()
            return original(data)
        monkeypatch.setattr(client, "_sock_send", send)
    return client, local, peer


@pytest.fixture
def robot_action_mqtt_gateway(monkeypatch: pytest.MonkeyPatch) -> RobotActionMqttGateway:
    result = RobotActionMqttGateway(None, None)
    monkeypatch.setattr(result._client, "is_connected", lambda: True)
    monkeypatch.setattr(asyncio.get_event_loop(), "getaddrinfo", AsyncMock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 1884))
    ]))
    return result


@pytest.mark.asyncio
async def test_cleanup_cannot_flush_backpressured_paho_publish_after_deadline(monkeypatch: pytest.MonkeyPatch, robot_action_mqtt_gateway: RobotActionMqttGateway) -> None:
    client, local, peer = connected_publisher(monkeypatch, robot_action_mqtt_gateway, block_first_send=True)
    monkeypatch.setattr(robot_action_mqtt_gateway, "_new_publisher", lambda: client)
    try:
        with pytest.raises(TimeoutError):
            await robot_action_mqtt_gateway.publish_command(command(), deadline=datetime.now(timezone.utc) + timedelta(seconds=0.025))
        assert local.fileno() == -1
        assert client._thread is None
        assert not robot_action_mqtt_gateway._client._out_messages
        # If disconnect ran before socket closure, Paho would flush the queued
        # PUBLISH on its second send and these bytes would contain movement.
        assert peer.recv(65536) == b""
    finally:
        local.close()
        peer.close()


@pytest.mark.asyncio
async def test_cancel_during_connect_joins_worker_before_return(monkeypatch: pytest.MonkeyPatch, robot_action_mqtt_gateway: RobotActionMqttGateway) -> None:
    client = robot_action_mqtt_gateway._new_publisher()
    entered, finished = threading.Event(), threading.Event()
    original_stop = client.loop_stop
    def connect(*args: object, **kwargs: object) -> mqtt.MQTTErrorCode:
        entered.set()
        time.sleep(0.04)
        return mqtt.MQTT_ERR_SUCCESS
    def stop() -> mqtt.MQTTErrorCode:
        finished.set()
        return original_stop()
    monkeypatch.setattr(client, "connect", connect)
    monkeypatch.setattr(client, "loop_stop", stop)
    publish = Mock(wraps=client.publish)
    monkeypatch.setattr(client, "publish", publish)
    monkeypatch.setattr(robot_action_mqtt_gateway, "_new_publisher", lambda: client)
    task = asyncio.create_task(robot_action_mqtt_gateway.publish_command(command()))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    assert not robot_action_mqtt_gateway._publications
    publish.assert_not_called()
    assert client._thread is None


@pytest.mark.asyncio
async def test_address_fallback_uses_fresh_client_before_any_publish(monkeypatch: pytest.MonkeyPatch, robot_action_mqtt_gateway: RobotActionMqttGateway) -> None:
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=[
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 1884, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 1884)),
    ]))
    first = robot_action_mqtt_gateway._new_publisher()
    first_connect = Mock(side_effect=ConnectionRefusedError())
    monkeypatch.setattr(first, "connect", first_connect)
    second, local, peer = connected_publisher(monkeypatch, robot_action_mqtt_gateway)
    def confirm(timeout: float) -> mqtt.MQTTErrorCode:
        for message in second._out_messages.values():
            message.info._set_as_published()
        return mqtt.MQTT_ERR_SUCCESS
    monkeypatch.setattr(second, "loop", confirm)
    factory = Mock(side_effect=[first, second])
    monkeypatch.setattr(robot_action_mqtt_gateway, "_new_publisher", factory)
    try:
        await robot_action_mqtt_gateway.publish_command(command())
        assert factory.call_count == 2
        assert first_connect.call_args.args[0] == "::1"
        assert not first._out_messages
        assert local.fileno() == -1
        assert peer.recv(65536).startswith(b"2")  # QoS1 PUBLISH packet.
    finally:
        local.close()
        peer.close()


async def forever() -> NoReturn:
    await asyncio.Event().wait()


def application_fixture(monkeypatch: pytest.MonkeyPatch, *, mqtt_fails_once: bool = False, block_setup: bool = False) -> SimpleNamespace:
    from app.cmd import main, robot_commands
    from app.repository.robot_action import RobotActionRepository

    registered = [uuid4(), uuid4()]
    attempts = {}
    events = []
    recovery = asyncio.Event()
    blocked = asyncio.Event()

    async def subscribe(*, queue_name: str, **_kwargs: object) -> SimpleNamespace:
        robot = queue_name.rsplit(".", 1)[-1]
        attempts[robot] = attempts.get(robot, 0) + 1
        if block_setup:
            blocked.set()
            try:
                await asyncio.Event().wait()
            finally:
                events.append("setup_cancelled")
        if robot == str(registered[1]) and attempts[robot] == 1:
            raise ConnectionError("Broker unavailable")
        if robot == str(registered[1]):
            recovery.set()
        return SimpleNamespace()

    rabbitmq_client = SimpleNamespace(subscribe=AsyncMock(side_effect=subscribe), close=AsyncMock(side_effect=lambda: events.append("broker_closed")))
    calls = []
    async def mqtt_start() -> None:
        calls.append("start")
        if mqtt_fails_once and len(calls) == 1:
            raise ConnectionError("MQTT temporarily unavailable")
    robot_action_mqtt_gateway = SimpleNamespace(start=AsyncMock(side_effect=mqtt_start), stop=AsyncMock(side_effect=lambda: events.append("mqtt_stopped")))
    async def transaction(self: Registry, callback: Callable[[None], Awaitable[T]]) -> T:
        return await callback(None)
    monkeypatch.setattr(main, "create_pg_engine", lambda: object())
    monkeypatch.setattr(main, "RedisClient", lambda: SimpleNamespace())
    monkeypatch.setattr(main, "RabbitMQClient", lambda: rabbitmq_client)
    monkeypatch.setattr(main.Registry, "transaction_wrapper", transaction)
    monkeypatch.setattr(RobotActionRepository, "list_robot_ids", AsyncMock(return_value=registered))
    monkeypatch.setattr(main, "RobotActionMqttGateway", lambda *args: robot_action_mqtt_gateway)
    monkeypatch.setattr(main, "RobotStatusMqttIngestion", lambda *args: SimpleNamespace(start=AsyncMock(), stop=AsyncMock()))
    monkeypatch.setattr(main, "init_topics", AsyncMock())
    monkeypatch.setattr(main.GroupService, "run_invitation_expiry_reconciler", lambda self: forever())
    monkeypatch.setattr(main.RobotActionService, "run_reconciler", lambda self: forever())
    monkeypatch.setattr(robot_commands, "ROBOT_COMMAND_STARTUP_RETRY_SECONDS", 0.001)
    auth_init = Mock(wraps=main.AuthMiddleware.init)
    monkeypatch.setattr(main.AuthMiddleware, "init", auth_init)
    application = main.App().application
    return SimpleNamespace(app=application, registered=registered, attempts=attempts, events=events,
                           recovery=recovery, blocked=blocked, rabbitmq_client=rabbitmq_client, robot_action_mqtt_gateway=robot_action_mqtt_gateway, auth_init=auth_init)


@pytest.mark.asyncio
@pytest.mark.parametrize("mqtt_fails_once", [False, True])
async def test_actual_application_startup_keeps_auth_routes_and_recovers_partial_robot_setup(monkeypatch: pytest.MonkeyPatch, mqtt_fails_once: bool) -> None:
    setup = application_fixture(monkeypatch, mqtt_fails_once=mqtt_fails_once)
    await setup.app.router.startup()
    try:
        setup.auth_init.assert_called_once()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup.app), base_url="http://test") as client:
            assert (await client.get("/api/v1/openapi.json")).status_code == 200
            response = await client.post(f"/api/v1/bots/{setup.registered[0]}/commands", json={"action": {
                "is_start": True, "is_finish": False, "direction": "NORTH"
            }})
            assert response.status_code == 401
        await asyncio.wait_for(setup.recovery.wait(), 1)
        await asyncio.wait_for(setup.app.state.robot_command_startup_task, 1)
        assert setup.attempts == {str(setup.registered[0]): 1, str(setup.registered[1]): 2}
        assert setup.app.state.robot_command_reconciler_task is not None
    finally:
        await setup.app.router.shutdown()
    assert setup.events.index("mqtt_stopped") < setup.events.index("broker_closed")
    assert setup.app.state.robot_command_reconciler_task.cancelled()


@pytest.mark.asyncio
async def test_actual_application_shutdown_joins_pending_command_setup_before_broker_close(monkeypatch: pytest.MonkeyPatch) -> None:
    setup = application_fixture(monkeypatch, block_setup=True)
    await setup.app.router.startup()
    await asyncio.wait_for(setup.blocked.wait(), 1)
    await setup.app.router.shutdown()
    assert setup.app.state.robot_command_startup_task.cancelled()
    assert setup.events.index("setup_cancelled") < setup.events.index("mqtt_stopped") < setup.events.index("broker_closed")
