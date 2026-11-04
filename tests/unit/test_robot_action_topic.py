from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.common.schemas.robot_action import CleaningAction, RobotAcknowledgement, RobotCommand
from app.external.mqtt.robot_action import RobotActionMqttGateway
from app.external.queues.queue import TopicMessage
from app.external.queues.topics.robot_action import RobotActionTopic


def command() -> RobotCommand:
    return RobotCommand(robot_id=uuid4(), command_id=uuid4(), attempt=1,
                        action=CleaningAction(is_start=True, is_finish=False, direction="EAST"))


@pytest.mark.asyncio
async def test_topic_uses_one_durable_serial_queue_per_robot() -> None:
    client = SimpleNamespace(subscribe=AsyncMock(), publish=AsyncMock(return_value="broker-id"))
    robot_action_topic = RobotActionTopic(client)
    payload = command()
    await asyncio.gather(robot_action_topic.ensure_robot(payload.robot_id), robot_action_topic.ensure_robot(payload.robot_id))
    client.subscribe.assert_awaited_once()
    options = client.subscribe.call_args.kwargs
    name = f"pleaco-backend.robot-actions.{payload.robot_id}"
    assert options["exchange_name"] == "pleco.robot.actions"
    assert options["routing_key"] == f"robot.{payload.robot_id}.*"
    assert options["queue_name"] == name
    assert options["durable"] is True
    assert options["prefetch_count"] == 1
    assert options["queue_arguments"] == {"x-single-active-consumer": True, "x-dead-letter-exchange": name + ".dlx"}
    assert options["retry_queue_arguments"] == {"x-message-ttl": 5000, "x-dead-letter-exchange": "", "x-dead-letter-routing-key": name}
    await robot_action_topic.publish_command(payload)
    assert client.publish.call_args.kwargs["routing_key"] == f"robot.{payload.robot_id}.command"
    assert client.publish.call_args.kwargs["correlation_id"] == str(payload.command_id)


@pytest.mark.asyncio
async def test_queue_validation_rejects_mismatch_and_accepts_typed_ack() -> None:
    robot_action_topic = RobotActionTopic(SimpleNamespace())
    handler = AsyncMock()
    robot_action_topic.set_handler(handler)
    payload = command()
    ack = RobotAcknowledgement(**payload.model_dump(), ack=True)
    await robot_action_topic._handle_message(TopicMessage(payload=ack.model_dump(mode="json"), routing_key=f"robot.{uuid4()}.ack"))
    await robot_action_topic._handle_message(TopicMessage(payload={}, routing_key=f"robot.{payload.robot_id}.ack"))
    handler.assert_not_awaited()
    await robot_action_topic._handle_message(TopicMessage(payload=ack.model_dump(mode="json"), routing_key=f"robot.{payload.robot_id}.ack"))
    assert isinstance(handler.call_args.args[0], RobotAcknowledgement)


@pytest.mark.asyncio
async def test_mqtt_ingress_requires_valid_identity_and_pending_command() -> None:
    payload = command()
    ack = RobotAcknowledgement(**payload.model_dump(), ack=True)
    robot_action_topic = SimpleNamespace(publish_ack=AsyncMock())
    robot_command_store = SimpleNamespace(active=AsyncMock(return_value=None))
    robot_action_mqtt_gateway = RobotActionMqttGateway(robot_action_topic, robot_command_store)
    own_topic = f"pleco/robots/{payload.robot_id}/acks"
    await robot_action_mqtt_gateway.handle_message(own_topic, ack.model_dump_json().encode())
    robot_action_topic.publish_ack.assert_not_awaited()
    robot_command_store.active.return_value = {"command_id": str(payload.command_id)}
    await robot_action_mqtt_gateway.handle_message(f"pleco/robots/{uuid4()}/acks", ack.model_dump_json().encode())
    await robot_action_mqtt_gateway.handle_message(own_topic, b"bad json")
    await robot_action_mqtt_gateway.handle_message(own_topic, json.dumps({**ack.model_dump(mode="json"), "ack": 1}).encode())
    robot_action_topic.publish_ack.assert_not_awaited()
    await robot_action_mqtt_gateway.handle_message(own_topic, ack.model_dump_json().encode())
    robot_action_topic.publish_ack.assert_awaited_once_with(ack)


@pytest.mark.asyncio
async def test_mqtt_ack_is_settled_only_after_queue_confirmation() -> None:
    robot_action_mqtt_gateway = RobotActionMqttGateway(None, None)
    client = Mock()
    message = SimpleNamespace(topic="unused", payload=b"unused", mid=3, qos=1)
    robot_action_mqtt_gateway.handle_message = AsyncMock(side_effect=ConnectionError())
    with pytest.raises(ConnectionError):
        await robot_action_mqtt_gateway._receive_message(client, message)
    client.ack.assert_not_called()
    robot_action_mqtt_gateway.handle_message.side_effect = None
    await robot_action_mqtt_gateway._receive_message(client, message)
    client.ack.assert_called_once_with(3, 1)
