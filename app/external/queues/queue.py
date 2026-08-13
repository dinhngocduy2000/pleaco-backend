"""Reusable, JSON-based RabbitMQ operations built on :mod:`aio_pika`."""

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import aio_pika
from aio_pika import DeliveryMode, ExchangeType, Message
from aio_pika.abc import (
    AbstractChannel,
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractRobustConnection,
    AbstractQueue,
)
from pydantic import BaseModel

from app.common.middleware.logger import Logger
from app.core.config import settings

logger = Logger()

JsonPayload = Mapping[str, Any] | BaseModel


@dataclass(frozen=True, slots=True)
class TopicMessage:
    """A decoded message supplied to a topic consumer."""

    payload: dict[str, Any]
    routing_key: str
    headers: Mapping[str, Any] = field(default_factory=dict)
    message_id: str | None = None
    correlation_id: str | None = None


MessageHandler = Callable[[TopicMessage], Awaitable[None]]


@dataclass(slots=True)
class TopicSubscription:
    """A cancellable consumer registered by :class:`RabbitMQClient`."""

    queue: AbstractQueue
    consumer_tag: str
    channel: AbstractChannel

    async def close(self) -> None:
        """Stop the consumer and release its dedicated channel."""
        await self.queue.cancel(self.consumer_tag)
        await self.channel.close()


class RabbitMQClient:
    """Own the robust RabbitMQ connection and common topic-exchange operations.

    Exchanges and queues are declared idempotently, so each independently deployed
    service can safely initialise its own publisher or consumer.
    """

    def __init__(
        self,
        url: str = settings.RABBITMQ_URL,
        connect_timeout: float = settings.RABBITMQ_CONNECT_TIMEOUT,
    ) -> None:
        self._url = url
        self._connect_timeout = connect_timeout
        self._connection: AbstractRobustConnection | None = None
        self._publisher_channel: AbstractChannel | None = None
        self._connect_lock = asyncio.Lock()
        self._subscriptions: list[TopicSubscription] = []

    async def connect(self) -> None:
        """Establish one auto-reconnecting AMQP connection."""
        if self._connection is not None and not self._connection.is_closed:
            return

        async with self._connect_lock:
            if self._connection is not None and not self._connection.is_closed:
                return
            self._connection = await aio_pika.connect_robust(
                self._url,
                timeout=self._connect_timeout,
                client_properties={"connection_name": "pleaco-backend"},
            )
            logger.info(msg="RabbitMQ connection established")

    async def close(self) -> None:
        """Stop consumers, close channels, and close the AMQP connection."""
        subscriptions, self._subscriptions = self._subscriptions, []
        for subscription in subscriptions:
            try:
                await subscription.close()
            except Exception:
                logger.exception(msg="Unable to close RabbitMQ consumer cleanly")

        if self._publisher_channel is not None and not self._publisher_channel.is_closed:
            await self._publisher_channel.close()
        self._publisher_channel = None

        if self._connection is not None and not self._connection.is_closed:
            await self._connection.close()
        self._connection = None
        logger.info(msg="RabbitMQ connection closed")

    async def publish(
        self,
        exchange_name: str,
        routing_key: str,
        payload: JsonPayload,
        *,
        headers: Mapping[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> str:
        """Publish a persistent JSON message and return its message ID."""
        exchange = await self._declare_exchange(exchange_name)
        message_id = str(uuid4())
        body = self._serialize_payload(payload)
        message = Message(
            body=body,
            content_type="application/json",
            delivery_mode=DeliveryMode.PERSISTENT,
            message_id=message_id,
            correlation_id=correlation_id,
            headers=dict(headers or {}),
            timestamp=datetime.now(timezone.utc),
        )
        await exchange.publish(message, routing_key=routing_key, mandatory=True)
        return message_id

    async def subscribe(
        self,
        exchange_name: str,
        queue_name: str,
        routing_key: str,
        handler: MessageHandler,
        *,
        prefetch_count: int = 10,
        durable: bool = True,
        requeue_on_error: bool = True,
    ) -> TopicSubscription:
        """Bind a durable queue to a topic exchange and start consuming it.

        A consumer gets its own channel, preventing its prefetch policy from
        affecting other consumers. Successful handlers acknowledge messages;
        handler errors are negatively acknowledged for retry by default.
        """
        channel = await self._new_channel(prefetch_count=prefetch_count)
        exchange = await channel.declare_exchange(
            exchange_name,
            ExchangeType.TOPIC,
            durable=True,
        )
        queue = await channel.declare_queue(queue_name, durable=durable)
        await queue.bind(exchange, routing_key=routing_key)

        async def consume(message: AbstractIncomingMessage) -> None:
            try:
                decoded_payload = self._deserialize_payload(message.body)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                logger.error(msg="Rejecting malformed RabbitMQ JSON message")
                await message.reject(requeue=False)
                return

            topic_message = TopicMessage(
                payload=decoded_payload,
                routing_key=message.routing_key,
                headers=message.headers or {},
                message_id=message.message_id,
                correlation_id=message.correlation_id,
            )
            try:
                await handler(topic_message)
            except Exception:
                logger.exception(
                    msg=(
                        "RabbitMQ topic handler failed "
                        f"for routing key '{message.routing_key}'"
                    )
                )
                await message.nack(requeue=requeue_on_error)
                return
            await message.ack()

        consumer_tag = await queue.consume(consume, no_ack=False)
        subscription = TopicSubscription(queue, consumer_tag, channel)
        self._subscriptions.append(subscription)
        return subscription

    async def _declare_exchange(self, exchange_name: str) -> AbstractExchange:
        channel = await self._publisher_channel_for_use()
        return await channel.declare_exchange(
            exchange_name,
            ExchangeType.TOPIC,
            durable=True,
        )

    async def _publisher_channel_for_use(self) -> AbstractChannel:
        if (
            self._publisher_channel is None
            or self._publisher_channel.is_closed
        ):
            self._publisher_channel = await self._new_channel()
        return self._publisher_channel

    async def _new_channel(self, prefetch_count: int | None = None) -> AbstractChannel:
        await self.connect()
        assert self._connection is not None
        channel = await self._connection.channel(publisher_confirms=True)
        if prefetch_count is not None:
            await channel.set_qos(prefetch_count=prefetch_count)
        return channel

    @staticmethod
    def _serialize_payload(payload: JsonPayload) -> bytes:
        if isinstance(payload, BaseModel):
            payload = payload.model_dump(mode="json")
        return json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")

    @staticmethod
    def _deserialize_payload(body: bytes) -> dict[str, Any]:
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("RabbitMQ message payload must be a JSON object")
        return payload
