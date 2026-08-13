"""A domain-friendly façade over RabbitMQ topic exchanges."""

from collections.abc import Mapping
from typing import Any

from app.external.queues.queue import (
    JsonPayload,
    MessageHandler,
    RabbitMQClient,
    TopicSubscription,
)


class Topic:
    """A named topic exchange that services can publish to or subscribe from.

    Define a small domain-specific subclass for each integration boundary, for
    example ``UserEventsTopic(queue_client)`` with an exchange name of
    ``"pleaco.user"``. Consumers should use a service-specific queue name so
    every service receives its own copy of matching events.
    """

    def __init__(self, client: RabbitMQClient, exchange_name: str) -> None:
        self._client = client
        self.exchange_name = exchange_name

    async def publish(
        self,
        routing_key: str,
        payload: JsonPayload,
        *,
        headers: Mapping[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> str:
        """Publish ``payload`` to this topic exchange."""
        return await self._client.publish(
            exchange_name=self.exchange_name,
            routing_key=routing_key,
            payload=payload,
            headers=headers,
            correlation_id=correlation_id,
        )

    async def subscribe(
        self,
        queue_name: str,
        routing_key: str,
        handler: MessageHandler,
        *,
        prefetch_count: int = 10,
        durable: bool = True,
        requeue_on_error: bool = True,
    ) -> TopicSubscription:
        """Subscribe ``handler`` through a queue owned by the calling service."""
        return await self._client.subscribe(
            exchange_name=self.exchange_name,
            queue_name=queue_name,
            routing_key=routing_key,
            handler=handler,
            prefetch_count=prefetch_count,
            durable=durable,
            requeue_on_error=requeue_on_error,
        )
