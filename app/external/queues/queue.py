"""Reusable, JSON-based RabbitMQ operations built on :mod:`aio_pika`."""

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from copy import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
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
RETRY_COUNT_HEADER = "x-retry-count"
ORIGINAL_ROUTING_KEY_HEADER = "x-original-routing-key"


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
            logger.info(msg="RabbitMQ Client - RabbitMQ connection established")

    async def close(self) -> None:
        """Stop consumers, close channels, and close the AMQP connection."""
        subscriptions, self._subscriptions = self._subscriptions, []
        for subscription in subscriptions:
            try:
                await subscription.close()
            except Exception:
                logger.exception(msg="Unable to close RabbitMQ consumer cleanly")

        if (
            self._publisher_channel is not None
            and not self._publisher_channel.is_closed
        ):
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
            headers={RETRY_COUNT_HEADER: 0, **dict(headers or {})},
            timestamp=datetime.now(timezone.utc),
        )
        await exchange.publish(
            message,
            routing_key=routing_key,
            mandatory=True,
            timeout=settings.RABBITMQ_PUBLISH_TIMEOUT,
        )
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
        max_retries: int | None = None,
    ) -> TopicSubscription:
        """Bind a durable queue to a topic exchange and start consuming it.

        A consumer gets its own channel, preventing its prefetch policy from
        affecting other consumers. Successful handlers acknowledge messages.
        Handler failures are republished to a delayed retry queue up to
        ``max_retries`` times. Broker policies must configure its TTL/return
        route and the source queue's DLX before this consumer starts.
        """
        retry_limit = settings.RABBITMQ_MAX_RETRIES if max_retries is None else max_retries
        if type(retry_limit) is not int or retry_limit < 0:
            raise ValueError("max_retries must be a non-negative integer")
        channel = await self._new_channel(prefetch_count=prefetch_count)

        consume = partial(
            self._consume_message,
            queue_name=queue_name,
            handler=handler,
            retry_limit=retry_limit,
            requeue_on_error=requeue_on_error,
        )

        try:
            exchange, dead_exchange, dead_queue, queue = await self._declare_subscription_topology(
                channel, exchange_name, queue_name, durable=durable,
            )
            await dead_queue.bind(dead_exchange, routing_key="")
            await queue.bind(exchange, routing_key=routing_key)
            consumer_tag = await queue.consume(consume, no_ack=False)
        except BaseException:
            await channel.close()
            raise
        subscription = TopicSubscription(queue, consumer_tag, channel)
        self._subscriptions.append(subscription)
        return subscription

    async def _declare_subscription_topology(
        self,
        channel: AbstractChannel,
        exchange_name: str,
        queue_name: str,
        *,
        durable: bool,
    ) -> tuple[AbstractExchange, AbstractExchange, AbstractQueue, AbstractQueue]:
        """Declare subscription resources and return those needed for binding."""
        # Settle every declaration before binding or closing the channel on
        # failure, so no sibling operation is left running during cleanup.
        declarations = await asyncio.gather(
            channel.declare_exchange(exchange_name, ExchangeType.TOPIC, durable=True),
            channel.declare_exchange(f"{queue_name}.dlx", ExchangeType.FANOUT, durable=True),
            channel.declare_queue(f"{queue_name}.dlq", durable=True),
            channel.declare_queue(f"{queue_name}.retry", durable=True),
            # Policies supply DLX arguments without changing the existing
            # source declaration or requiring backlog deletion.
            channel.declare_queue(queue_name, durable=durable),
            return_exceptions=True,
        )
        for declaration in declarations:
            if isinstance(declaration, BaseException):
                raise declaration
        exchange, dead_exchange, dead_queue, _retry_queue, queue = declarations
        return exchange, dead_exchange, dead_queue, queue

    async def _consume_message(
        self,
        message: AbstractIncomingMessage,
        *,
        queue_name: str,
        handler: MessageHandler,
        retry_limit: int,
        requeue_on_error: bool,
    ) -> None:
        """Decode and settle one delivery using the subscription's retry policy."""
        headers = dict(message.headers or {})
        retry_count = headers.get(RETRY_COUNT_HEADER, 0)
        context = f"queue='{queue_name}' message_id='{message.message_id}'"
        if type(retry_count) is not int or retry_count < 0:
            logger.error(msg=f"Dead-lettering invalid RabbitMQ retry count {context}")
            await message.reject(requeue=False)
            return
        context += f" retry_count={retry_count}"
        original_key = headers.get(ORIGINAL_ROUTING_KEY_HEADER, message.routing_key)
        if not isinstance(original_key, str):
            logger.error(msg=f"Dead-lettering invalid RabbitMQ routing header {context}")
            await message.reject(requeue=False)
            return
        try:
            decoded_payload = self._deserialize_payload(message.body)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            logger.error(msg=f"Dead-lettering malformed RabbitMQ JSON {context}")
            await message.reject(requeue=False)
            return

        topic_message = TopicMessage(
            payload=decoded_payload,
            routing_key=original_key,
            headers=headers,
            message_id=message.message_id,
            correlation_id=message.correlation_id,
        )
        try:
            await handler(topic_message)
        except Exception as error:
            # Exception text/tracebacks can contain payload values (e.g. OTPs).
            logger.warning(
                msg=f"RabbitMQ handler failed {context} error_type={type(error).__name__}"
            )
            if not requeue_on_error or retry_count >= retry_limit:
                logger.error(msg=f"Dead-lettering failed RabbitMQ message {context}")
                await message.reject(requeue=False)
                return
            retry_message = copy(message)
            retry_message.headers = {
                key: value
                for key, value in headers.items()
                if key not in ("CC", "BCC")
            }
            retry_message.headers[RETRY_COUNT_HEADER] = retry_count + 1
            retry_message.headers[ORIGINAL_ROUTING_KEY_HEADER] = original_key
            retry_message.message_id = message.message_id or str(uuid4())
            try:
                publisher = await self._publisher_channel_for_use()
                await publisher.default_exchange.publish(
                    retry_message,
                    routing_key=f"{queue_name}.retry",
                    mandatory=True,
                    timeout=settings.RABBITMQ_PUBLISH_TIMEOUT,
                )
            except Exception as publish_error:
                logger.error(
                    msg=(
                        f"RabbitMQ retry publish failed; retaining original {context} "
                        f"error_type={type(publish_error).__name__}"
                    )
                )
                try:
                    if not message.channel.is_closed:
                        await message.nack(requeue=True)
                except aio_pika.exceptions.ChannelInvalidStateError:
                    # The original delivery belongs to a closed channel and
                    # is recovered by RabbitMQ, even after robust reconnect.
                    pass
                return
            # Keep acknowledgement errors out of publish-error recovery.
            await message.ack()
            logger.info(
                msg=(
                    f"RabbitMQ retry scheduled queue='{queue_name}' "
                    f"message_id='{retry_message.message_id}' retry_count={retry_count + 1}"
                )
            )
            return
        await message.ack()

    async def _declare_exchange(self, exchange_name: str) -> AbstractExchange:
        """Idempotently declare and return a durable topic exchange for publishing.

        Declaration occurs on the shared publisher channel. RabbitMQ treats a
        compatible redeclaration as a no-op, allowing publishers to initialise
        independently and allowing aio-pika to restore the exchange after a
        reconnect.
        """
        channel = await self._publisher_channel_for_use()
        return await channel.declare_exchange(
            exchange_name,
            ExchangeType.TOPIC,
            durable=True,
        )

    async def _publisher_channel_for_use(self) -> AbstractChannel:
        """Return the reusable publisher channel, recreating it when necessary.

        A channel can close independently from the robust connection (for
        example, following a broker-side channel exception). A fresh robust
        channel is created lazily so subsequent publishes can recover.
        """
        if self._publisher_channel is None or self._publisher_channel.is_closed:
            self._publisher_channel = await self._new_channel()
        return self._publisher_channel

    async def _new_channel(self, prefetch_count: int | None = None) -> AbstractChannel:
        """Open a publisher-confirming robust channel on the live connection.

        ``prefetch_count`` is set only for consumer channels to cap the number
        of unacknowledged deliveries handled concurrently by that consumer.
        Publisher confirms make ``publish`` wait until RabbitMQ accepts or
        rejects the message.
        """
        await self.connect()
        assert self._connection is not None
        channel = await self._connection.channel(
            publisher_confirms=True, on_return_raises=True,
        )
        try:
            if prefetch_count is not None:
                await channel.set_qos(prefetch_count=prefetch_count)
        except BaseException:
            await channel.close()
            raise
        return channel

    @staticmethod
    def _serialize_payload(payload: JsonPayload) -> bytes:
        """Convert a mapping or Pydantic model into compact UTF-8 JSON bytes.

        Pydantic's JSON mode preserves JSON-compatible representations for
        values such as datetimes and UUIDs. ``default=str`` makes plain mapping
        payloads tolerant of equivalent non-JSON-native values.
        """
        if isinstance(payload, BaseModel):
            payload = payload.model_dump(mode="json")
        return json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")

    @staticmethod
    def _deserialize_payload(body: bytes) -> dict[str, Any]:
        """Decode a UTF-8 JSON object received from RabbitMQ.

        Topic payloads are intentionally restricted to JSON objects so message
        handlers always receive named fields. Invalid UTF-8/JSON is surfaced to
        the consumer callback, which rejects the delivery without requeuing it.
        """
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("RabbitMQ message payload must be a JSON object")
        return payload
