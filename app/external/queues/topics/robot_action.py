"""Shared command and acknowledgement queues for robot actions."""

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

from pydantic import ValidationError

from app.common.middleware.logger import Logger
from app.common.schemas.robot_action import RobotAcknowledgement, RobotCommand
from app.external.queues.queue import RabbitMQClient, TopicMessage
from app.external.queues.topics.base import Topic

logger = Logger()


class RobotActionTopic(Topic):
    exchange_name = "pleco.robot.actions"
    command_queue_name = "pleaco-backend.robot-actions.commands"
    ack_queue_name = "pleaco-backend.robot-actions.acks"

    def __init__(self, rabbitmq_client: RabbitMQClient) -> None:
        super().__init__(rabbitmq_client, self.exchange_name)
        self._subscriptions = {}
        self._lock = asyncio.Lock()
        self._handler = None

    def set_handler(self, handler: Callable[[RobotCommand], Awaitable[None]]) -> None:
        self._handler = handler

    @property
    def ready(self) -> bool:
        return len(self._subscriptions) == 2

    async def start(self) -> None:
        """Establish the two fixed consumers, retaining successful partial setup."""
        async with self._lock:
            # Bind ACKs first so command dispatch cannot precede their route.
            for queue_name, routing_key, handler in (
                (self.ack_queue_name, "robot.*.ack", self._handle_ack),
                (self.command_queue_name, "robot.*.command", self._handle_command),
            ):
                if queue_name in self._subscriptions:
                    continue
                subscription = await self.subscribe(
                    queue_name=queue_name,
                    routing_key=routing_key,
                    handler=handler,
                    prefetch_count=1,
                    queue_arguments={
                        "x-single-active-consumer": True,
                        "x-dead-letter-exchange": f"{queue_name}.dlx",
                    },
                    retry_queue_arguments={
                        "x-message-ttl": 5000,
                        "x-dead-letter-exchange": "",
                        "x-dead-letter-routing-key": queue_name,
                    },
                )
                self._subscriptions[queue_name] = subscription

    async def publish_command(self, command: RobotCommand) -> str:
        return await self.publish(
            f"robot.{command.robot_id}.command", command,
            correlation_id=str(command.command_id),
        )

    async def publish_ack(self, ack: RobotAcknowledgement) -> str:
        return await self.publish(
            f"robot.{ack.robot_id}.ack", ack,
            correlation_id=str(ack.command_id),
        )

    async def _handle_command(self, message: TopicMessage) -> None:
        await self._handle_message(message, expected_kind="command")

    async def _handle_ack(self, message: TopicMessage) -> None:
        await self._handle_message(message, expected_kind="ack")

    async def _handle_message(self, message: TopicMessage, *, expected_kind: str) -> None:
        try:
            parts = message.routing_key.split(".")
            if len(parts) != 3 or parts[0] != "robot" or parts[2] != expected_kind:
                raise ValueError("Unexpected robot action route")
            model = RobotAcknowledgement if expected_kind == "ack" else RobotCommand
            command = model.model_validate(message.payload)
            if command.robot_id != UUID(parts[1]):
                raise ValueError("Robot action route/payload mismatch")
        except (ValueError, ValidationError):
            logger.warning(msg="Ignoring invalid queued robot action")
            return
        await self._handler(command)
