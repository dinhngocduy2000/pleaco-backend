"""Shared command and acknowledgement queues for robot actions."""

import asyncio
from collections.abc import Awaitable, Callable

from app.common.schemas.robot_action import RobotAcknowledgement, RobotCommand
from app.common.utils.robot_actions import handle_robot_action_message, publish_robot_action
from app.external.queues.queue import RabbitMQClient, TopicMessage
from app.external.queues.topics.base import Topic


class RobotActionTopic(Topic):
    exchange_name = "pleco.robot.actions"
    command_queue_name = "pleaco-backend.robot-actions.commands"
    ack_queue_name = "pleaco-backend.robot-actions.acks"

    def __init__(self, rabbitmq_client: RabbitMQClient) -> None:
        super().__init__(rabbitmq_client, self.exchange_name)
        self._subscriptions = {}
        self._lock = asyncio.Lock()
        self._command_handler: Callable[[RobotCommand], Awaitable[None]] | None = None
        self._ack_handler: Callable[[RobotAcknowledgement], Awaitable[None]] | None = (
            None
        )

    def set_handlers(
        self,
        command_handler: Callable[[RobotCommand], Awaitable[None]],
        ack_handler: Callable[[RobotAcknowledgement], Awaitable[None]],
    ) -> None:
        self._command_handler = command_handler
        self._ack_handler = ack_handler

    def set_handler(self, handler: Callable[[RobotCommand], Awaitable[None]]) -> None:
        """Keep the original single-handler registration for existing callers."""
        self.set_handlers(handler, handler)

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
        return await publish_robot_action(self, command, "command")

    async def publish_ack(self, ack: RobotAcknowledgement) -> str:
        return await publish_robot_action(self, ack, "ack")

    async def _handle_command(self, message: TopicMessage) -> None:
        await self._handle_message(message, expected_queue_type="command")

    async def _handle_ack(self, message: TopicMessage) -> None:
        await self._handle_message(message, expected_queue_type="ack")

    async def _handle_message(
        self, message: TopicMessage, *, expected_queue_type: str
    ) -> None:
        await handle_robot_action_message(
            message, expected_queue_type=expected_queue_type,
            command_handler=self._command_handler, ack_handler=self._ack_handler,
        )
