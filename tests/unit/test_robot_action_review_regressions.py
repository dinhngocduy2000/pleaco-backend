"""Independent regressions for stale MQTT publications, using installed Paho."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import socket
import threading
import time
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import paho.mqtt.client as mqtt
import pytest

from app.common.schemas.robot_action import CleaningAction, RobotCommand
from app.external.mqtt.robot_action import RobotActionMqttGateway


def movement() -> RobotCommand:
    return RobotCommand(
        robot_id=uuid4(), command_id=uuid4(), attempt=1,
        action=CleaningAction(is_start=True, is_finish=False, direction="NORTH"),
    )


def publisher_harness(monkeypatch: pytest.MonkeyPatch, *, send_rc: mqtt.MQTTErrorCode) -> tuple[RobotActionMqttGateway, mqtt.Client]:
    """Use real Paho publish/buffering, replacing only socket operations."""
    robot_action_mqtt_gateway = RobotActionMqttGateway(None, None)
    monkeypatch.setattr(robot_action_mqtt_gateway._client, "is_connected", lambda: True)
    publisher = robot_action_mqtt_gateway._new_publisher()
    monkeypatch.setattr(publisher, "is_connected", lambda: True)

    def connect(*_args: object, **_kwargs: object) -> mqtt.MQTTErrorCode:
        if publisher.on_connect is not None:
            publisher.on_connect(publisher, None, None, 0, None)
        return mqtt.MQTT_ERR_SUCCESS

    monkeypatch.setattr(publisher, "connect", Mock(side_effect=connect))
    monkeypatch.setattr(publisher, "disconnect", Mock(return_value=mqtt.MQTT_ERR_SUCCESS))
    monkeypatch.setattr(publisher, "_send_publish", Mock(return_value=send_rc))
    monkeypatch.setattr(publisher, "loop", Mock(return_value=mqtt.MQTT_ERR_SUCCESS))
    monkeypatch.setattr(robot_action_mqtt_gateway, "_new_publisher", Mock(return_value=publisher))
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 1883)),
    ]))
    return robot_action_mqtt_gateway, publisher


@pytest.mark.asyncio
async def test_failed_real_paho_publish_cannot_replay_on_ack_subscriber_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    robot_action_mqtt_gateway, publisher = publisher_harness(monkeypatch, send_rc=mqtt.MQTT_ERR_NO_CONN)
    with pytest.raises(ConnectionError):
        await robot_action_mqtt_gateway.publish_command(movement())
    # Paho really buffered this failed QoS 1 packet: the original bug's premise.
    assert len(publisher._out_messages) == 1
    assert publisher._reconnect_on_failure is False
    publisher.disconnect.assert_called_once()
    assert publisher._thread is None
    # The persistent client that actually reconnects never owns movement packets.
    robot_action_mqtt_gateway._client._messages_reconnect_reset_out()
    assert not robot_action_mqtt_gateway._client._out_messages


@pytest.mark.asyncio
async def test_unconfirmed_real_paho_publish_is_disposed_at_command_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    robot_action_mqtt_gateway, publisher = publisher_harness(monkeypatch, send_rc=mqtt.MQTT_ERR_SUCCESS)
    with pytest.raises(TimeoutError):
        await robot_action_mqtt_gateway.publish_command(
            movement(), deadline=datetime.now(timezone.utc) + timedelta(milliseconds=40),
        )
    publisher.disconnect.assert_called_once()
    assert publisher._reconnect_on_failure is False
    assert publisher._thread is None
    assert not robot_action_mqtt_gateway._client._out_messages


@pytest.mark.asyncio
async def test_async_cancellation_joins_publisher_worker_before_returning(monkeypatch: pytest.MonkeyPatch) -> None:
    robot_action_mqtt_gateway, publisher = publisher_harness(monkeypatch, send_rc=mqtt.MQTT_ERR_SUCCESS)
    worker_entered = threading.Event()

    def network_iteration(*_args: object, **_kwargs: object) -> mqtt.MQTTErrorCode:
        worker_entered.set()
        time.sleep(0.01)
        return mqtt.MQTT_ERR_SUCCESS

    publisher.loop.side_effect = network_iteration
    task = asyncio.create_task(robot_action_mqtt_gateway.publish_command(movement()))
    assert await asyncio.to_thread(worker_entered.wait, 1), "Publisher did not enter its network loop"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    publisher.disconnect.assert_called_once()
    calls_after_cancellation = publisher.loop.call_count
    await asyncio.sleep(0.04)
    assert publisher.loop.call_count == calls_after_cancellation
    assert not robot_action_mqtt_gateway._client._out_messages
