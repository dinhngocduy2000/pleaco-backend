"""A domain-friendly façade over RabbitMQ topic exchanges."""

import asyncio
from collections.abc import Mapping
from typing import Awaitable, Callable
from aio_pika.abc import FieldValue

from app.common.middleware.logger import Logger
from app.external.queues.queue import (
    JsonPayload,
    MessageHandler,
    RabbitMQClient,
    TopicSubscription,
)
logger = Logger()

async def init_topics(
        consumers: Mapping[str, Callable[[], Awaitable[None]]]
    ) -> None:
        """Start independent RabbitMQ consumers concurrently during application startup."""
        consumer_names = list(consumers)
        results = await asyncio.gather(
            *(consumer() for consumer in consumers.values()),
            return_exceptions=True,
        )
        for consumer_name, result in zip(consumer_names, results):
            if isinstance(result, Exception):
                logger.error(
                    msg=f"Unable to start {consumer_name} consumer: {result}"
                )

class Topic:
    """A named topic exchange that services can publish to or subscribe from.

    Define a small domain-specific subclass for each integration boundary, for
    example ``UserEventsTopic(queue_client)`` with an exchange name of
    ``"pleaco.user"``. Consumers should use a service-specific queue name so
    every service receives its own copy of matching events.
    """

    def __init__(self, rabbitmq_client: RabbitMQClient, exchange_name: str) -> None:
        self._rabbitmq_client = rabbitmq_client
        self.exchange_name = exchange_name

    async def publish(
        self,
        routing_key: str,
        payload: JsonPayload,
        *,
        headers: Mapping[str, FieldValue] | None = None,
        correlation_id: str | None = None,
    ) -> str:
        """Publish ``payload`` to this topic exchange."""
        return await self._rabbitmq_client.publish(
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
        max_retries: int | None = None,
        queue_arguments: Mapping[str, FieldValue] | None = None,
        retry_queue_arguments: Mapping[str, FieldValue] | None = None,
    ) -> TopicSubscription:
        """Subscribe ``handler`` through a queue owned by the calling service."""
        return await self._rabbitmq_client.subscribe(
            exchange_name=self.exchange_name,
            queue_name=queue_name,
            routing_key=routing_key,
            handler=handler,
            prefetch_count=prefetch_count,
            durable=durable,
            requeue_on_error=requeue_on_error,
            max_retries=max_retries,
            **({"queue_arguments": queue_arguments} if queue_arguments is not None else {}),
            **({"retry_queue_arguments": retry_queue_arguments} if retry_queue_arguments is not None else {}),
        )
