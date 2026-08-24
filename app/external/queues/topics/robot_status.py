"""RabbitMQ topic for normalized robot status events."""

from app.common.schemas.robot_status import RobotStatusEvent
from app.external.queues.queue import RabbitMQClient, TopicMessage
from app.external.queues.topics.base import Topic


class RobotStatusTopic(Topic):
    exchange_name = "pleco.robot.events"
    connection_routing_key = "robot.connection.changed"
    status_routing_key = "robot.status.changed"
    queue_name = "pleaco-backend.robot-status"

    def __init__(self, client: RabbitMQClient, service) -> None:
        super().__init__(client, self.exchange_name)
        self._service = service

    async def publish_status(self, event: RobotStatusEvent) -> str:
        routing_key = (
            self.connection_routing_key
            if event.type == "robot.connection.changed"
            else self.status_routing_key
        )
        return await self.publish(routing_key, event, correlation_id=str(event.robot_id))

    async def start_consumer(self) -> None:
        await self.subscribe(
            queue_name=self.queue_name,
            routing_key="robot.#",
            handler=self._handle_message,
            prefetch_count=1,
        )

    async def _handle_message(self, message: TopicMessage) -> None:
        await self._service.process_status_event(RobotStatusEvent.model_validate(message.payload))
