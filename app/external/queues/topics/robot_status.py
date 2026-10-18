"""RabbitMQ topic for normalized robot status events."""

from aio_pika import ExchangeType

from app.common.schemas.robot_status import RobotStatusEvent
from app.external.queues.queue import RabbitMQClient, TopicMessage
from app.external.queues.topics.base import Topic


class RobotStatusTopic(Topic):
    exchange_name = "pleco.robot.events"
    connection_routing_key = "robot.connection.changed"
    queue_name = "pleaco-backend.robot-status"

    def __init__(self, client: RabbitMQClient, service) -> None:
        super().__init__(client, self.exchange_name)
        self._service = service

    async def publish_status(self, event: RobotStatusEvent) -> str:
        if event.type != self.connection_routing_key:
            raise ValueError("RabbitMQ robot topic accepts connectivity events only")
        return await self.publish(
            self.connection_routing_key, event, correlation_id=str(event.robot_id)
        )

    async def start_consumer(self) -> None:
        subscription = await self.subscribe(
            queue_name=self.queue_name,
            routing_key=self.connection_routing_key,
            handler=self._handle_message,
            prefetch_count=1,
        )
        # Queue bindings survive deploys. Remove the old broad binding after
        # adding the new one so heartbeat/status traffic no longer enters it.
        exchange = await subscription.channel.declare_exchange(
            self.exchange_name, ExchangeType.TOPIC, durable=True
        )
        await subscription.queue.unbind(exchange, routing_key="robot.#")

    async def _handle_message(self, message: TopicMessage) -> None:
        if message.routing_key != self.connection_routing_key:
            return
        event = RobotStatusEvent.model_validate(message.payload)
        if event.type != self.connection_routing_key:
            return
        await self._service.process_status_event(event)
