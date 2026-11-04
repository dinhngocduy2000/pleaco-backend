"""Reusable per-robot action topic, with ordered command/ACK delivery."""

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

    def __init__(self, rabbitmq_client: RabbitMQClient) -> None:
        super().__init__(rabbitmq_client, self.exchange_name)
        self._subscriptions = {}
        self._lock = asyncio.Lock()
        self._handler = None

    def set_handler(self, handler: Callable[[RobotCommand], Awaitable[None]]) -> None:
        self._handler = handler

    async def ensure_robot(self, robot_id: UUID) -> None:
        async with self._lock:
            if robot_id in self._subscriptions:
                return
            queue_name = f"pleaco-backend.robot-actions.{robot_id}"
            topic_subscription = await self.subscribe(
                queue_name=queue_name,
                routing_key=f"robot.{robot_id}.*",
                handler=self._handle_message,
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
            self._subscriptions[robot_id] = topic_subscription

    async def publish_command(self, command: RobotCommand) -> str:
        await self.ensure_robot(command.robot_id)
        return await self.publish(
            f"robot.{command.robot_id}.command", command,
            correlation_id=str(command.command_id),
        )

    async def publish_ack(self, ack: RobotAcknowledgement) -> str:
        await self.ensure_robot(ack.robot_id)
        return await self.publish(
            f"robot.{ack.robot_id}.ack", ack,
            correlation_id=str(ack.command_id),
        )

    async def _handle_message(self, message: TopicMessage) -> None:
        try:
            parts = message.routing_key.split(".")
            if len(parts) != 3 or parts[0] != "robot" or parts[2] not in {"command", "ack"}:
                raise ValueError("Unexpected robot action route")
            model = RobotAcknowledgement if parts[2] == "ack" else RobotCommand
            command = model.model_validate(message.payload)
            if command.robot_id != UUID(parts[1]):
                raise ValueError("Robot action route/payload mismatch")
        except (ValueError, ValidationError):
            logger.warning(msg="Ignoring invalid queued robot action")
            return
        await self._handler(command)
