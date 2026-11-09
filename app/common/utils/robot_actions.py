"""Routing and validation helpers for queued robot actions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Generic, NamedTuple, Protocol, TypeVar
from uuid import UUID

from pydantic import ValidationError

from app.common.middleware.logger import Logger
from app.common.schemas.robot_action import (
    CommandStatus, RobotAcknowledgement, RobotCommand, RobotCommandRecord, RobotPositionRecord,
)
from app.external.queues.queue import TopicMessage
from app.external.queues.topics.base import Topic

if TYPE_CHECKING:
    from app.external.mqtt.robot_action import RobotActionMqttGateway
    from app.external.queues.topics.robot_action import RobotActionTopic
    from app.external.redis.robot_commands import RobotCommandStore

logger = Logger()
MessageT = TypeVar("MessageT", bound=RobotCommand)


class FinishCommand(Protocol):
    def __call__(
        self, record: RobotCommandRecord, status: CommandStatus,
        reason: str | None = None, *, position: RobotPositionRecord | None = None,
    ) -> Awaitable[bool]: ...


class RobotActionRoute(NamedTuple, Generic[MessageT]):
    model: type[MessageT]
    handler: Callable[[MessageT], Awaitable[None]] | None

    async def deliver(self, payload: object, robot_id: UUID, kind: str) -> None:
        try:
            command = self.model.model_validate(payload)
            if command.robot_id != robot_id:
                raise ValueError("Robot action route/payload mismatch")
        except (ValueError, ValidationError):
            logger.warning(msg="Ignoring invalid queued robot action")
            return
        if self.handler is None:
            raise RuntimeError(f"Robot action {kind} handler is not registered")
        await self.handler(command)


async def publish_robot_action(topic: Topic, command: RobotCommand, kind: str) -> str:
    """Publish a command or acknowledgement with its established route and ID."""
    return await topic.publish(
        f"robot.{command.robot_id}.{kind}", command,
        correlation_id=str(command.command_id),
    )


async def handle_robot_action_message(
    message: TopicMessage, *, expected_queue_type: str,
    command_handler: Callable[[RobotCommand], Awaitable[None]] | None,
    ack_handler: Callable[[RobotAcknowledgement], Awaitable[None]] | None,
) -> None:
    """Validate a queued route and payload before invoking its typed handler."""
    routes: dict[str, RobotActionRoute[RobotCommand] | RobotActionRoute[RobotAcknowledgement]] = {
        "command": RobotActionRoute(RobotCommand, command_handler),
        "ack": RobotActionRoute(RobotAcknowledgement, ack_handler),
    }
    try:
        prefix, robot_id_text, queue_type = message.routing_key.split(".")
        route = routes.get(expected_queue_type)
        if route is None or prefix != "robot" or queue_type != expected_queue_type:
            raise ValueError("Unexpected robot action route")
        robot_id = UUID(robot_id_text)
    except (ValueError, ValidationError):
        logger.warning(msg="Ignoring invalid queued robot action")
        return
    await route.deliver(message.payload, robot_id, expected_queue_type)


def to_robot_command(record: RobotCommandRecord) -> RobotCommand:
    """Extract the broker/device envelope from a saved command."""
    return RobotCommand.model_validate({key: record[key] for key in RobotCommand.model_fields})


async def validate_active_command(
    message: RobotCommand, store: RobotCommandStore,
    can_process: Callable[[RobotCommandRecord], Awaitable[bool]],
) -> RobotCommandRecord | None:
    """Match the active envelope, then enforce command eligibility."""
    active_command = await store.active(message.robot_id)
    if active_command is None:
        return None
    saved_command = {field: active_command[field] for field in RobotCommand.model_fields}
    received_command = message.model_dump(mode="json", exclude={"ack"})
    if saved_command != received_command or not await can_process(active_command):
        return None
    return active_command


async def dispatch_robot_command(
    message: RobotCommand, active_command: RobotCommandRecord, *,
    store: RobotCommandStore, gateway: RobotActionMqttGateway,
    clock: Callable[[], datetime], command_deadline: Callable[[], str],
) -> None:
    """Save dispatch intent, publish through MQTT, and mark confirmation."""
    if active_command["status"] == CommandStatus.QUEUED.value:
        pending_command: RobotCommandRecord = {
            **active_command,
            "status": CommandStatus.AWAITING_ACK.value,
            "updated_at": clock().isoformat(),
            "deadline": command_deadline(),
        }
        if not await store.transition(active_command, pending_command):
            return
    elif (
        active_command["status"] == CommandStatus.AWAITING_ACK.value
        and not await store.dispatched(active_command)
    ):
        pending_command = active_command
    else:
        return
    try:
        await gateway.publish_command(message, deadline=datetime.fromisoformat(pending_command["deadline"]))
        await store.mark_dispatched(pending_command)
    except Exception:
        await store.transition(pending_command, active_command)
        raise


async def apply_robot_ack(
    message: RobotAcknowledgement, command: RobotCommandRecord, *,
    retry_enabled: bool, on_success_ack: Callable[[RobotCommandRecord], RobotPositionRecord | None] | None,
    finish_command: FinishCommand, store: RobotCommandStore,
    topic: RobotActionTopic, clock: Callable[[], datetime], command_deadline: Callable[[], str],
) -> None:
    """Apply an eligible ACK, preserving atomic completion and retry publication."""
    if command["status"] != CommandStatus.AWAITING_ACK.value:
        return
    if message.ack:
        position = on_success_ack(command) if on_success_ack is not None else None
        await finish_command(command, CommandStatus.SUCCEEDED, position=position)
    elif not retry_enabled or message.attempt >= 4:
        reason = "Robot action failed after three retries" if retry_enabled else "Robot action failed; retries disabled"
        await finish_command(command, CommandStatus.FAILED, reason)
    else:
        retry_command: RobotCommandRecord = {
            **command,
            "attempt": message.attempt + 1,
            "status": CommandStatus.QUEUED.value,
            "updated_at": clock().isoformat(),
            "deadline": command_deadline(),
        }
        if await store.transition(command, retry_command):
            await topic.publish_command(to_robot_command(retry_command))
