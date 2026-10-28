import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aio_pika
import pytest
from pydantic import BaseModel, ValidationError
from pamqp.commands import Basic

from app.core.config import Settings, settings
from app.external.queues.queue import RabbitMQClient
from app.external.queues.topics import Topic


class EventPayload(BaseModel):
    event_id: int


def test_serializes_mapping_and_pydantic_payloads() -> None:
    assert RabbitMQClient._serialize_payload({"event_id": 1}) == b'{"event_id":1}'
    assert RabbitMQClient._serialize_payload(EventPayload(event_id=2)) == b'{"event_id":2}'


def test_rejects_non_object_payloads() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        RabbitMQClient._deserialize_payload(json.dumps(["not", "an object"]).encode())


@pytest.mark.asyncio
async def test_topic_delegates_publish_to_its_exchange() -> None:
    class ClientStub:
        async def publish(self, **kwargs):
            return kwargs

    topic = Topic(ClientStub(), "pleaco.user")  # type: ignore[arg-type]
    result = await topic.publish("user.created", {"id": "123"})

    assert result["exchange_name"] == "pleaco.user"
    assert result["routing_key"] == "user.created"


class Incoming(aio_pika.Message):
    """Use aio-pika's real message copying, with mocked delivery operations."""

    def __init__(self, body=b'{ "event_id": 1 }', headers=None, message_id="stable-id"):
        super().__init__(
            body, headers=headers, message_id=message_id,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            correlation_id="correlation", content_type="application/json",
            content_encoding="utf-8", priority=3, reply_to="reply",
            timestamp=1700000000, type="event", app_id="pleaco",
        )
        self.routing_key = "event.created"
        self.channel = SimpleNamespace(is_closed=False)
        self.ack = AsyncMock()
        self.nack = AsyncMock()
        self.reject = AsyncMock()


@pytest.fixture
def broker():
    queues = {}

    async def declare_queue(name, **kwargs):
        queue = queues.setdefault(name, SimpleNamespace(
            bind=AsyncMock(), consume=AsyncMock(return_value="consumer-tag"),
        ))
        return queue

    publisher = SimpleNamespace(default_exchange=SimpleNamespace(publish=AsyncMock()))
    channel = SimpleNamespace(
        declare_exchange=AsyncMock(), declare_queue=AsyncMock(side_effect=declare_queue),
        close=AsyncMock(), is_closed=False,
    )
    client = RabbitMQClient()
    client._new_channel = AsyncMock(return_value=channel)
    client._publisher_channel_for_use = AsyncMock(return_value=publisher)
    return SimpleNamespace(client=client, channel=channel, publisher=publisher, queues=queues)


async def subscribe(broker, handler=None, **options):
    handler = handler or AsyncMock(side_effect=RuntimeError("sensitive payload"))
    await broker.client.subscribe("events", "service.events", "event.#", handler, **options)
    callback = broker.queues["service.events"].consume.call_args.args[0]
    return callback, handler


@pytest.mark.asyncio
async def test_success_acknowledges_without_retry(broker):
    consume, handler = await subscribe(broker, AsyncMock())
    message = Incoming()
    await consume(message)
    message.ack.assert_awaited_once()
    message.nack.assert_not_awaited()
    broker.publisher.default_exchange.publish.assert_not_awaited()
    assert handler.call_args.args[0].routing_key == "event.created"


@pytest.mark.asyncio
async def test_independent_counts_and_metadata_survive_retries(broker):
    consume, handler = await subscribe(broker)
    original = Incoming(headers={"custom": "value", "CC": ["other"], "BCC": ["hidden"]})
    current = original
    for count in (1, 2, 3):
        await consume(current)
        current.ack.assert_awaited_once()
        published = broker.publisher.default_exchange.publish.call_args.args[0]
        assert published.headers["x-retry-count"] == count
        assert published.headers["x-original-routing-key"] == "event.created"
        assert published.headers["custom"] == "value"
        assert "CC" not in published.headers and "BCC" not in published.headers
        for key in ("body", "message_id", "correlation_id", "delivery_mode",
                    "content_type", "content_encoding", "priority", "reply_to",
                    "timestamp", "type", "app_id"):
            assert getattr(published, key) == getattr(original, key)
        assert handler.call_args.args[0].routing_key == "event.created"
        current = Incoming(headers=dict(published.headers))
        current.routing_key = "service.events"
    await consume(current)
    current.reject.assert_awaited_once_with(requeue=False)
    current.ack.assert_not_awaited()
    assert broker.publisher.default_exchange.publish.await_count == 3
    assert handler.await_count == 4
    # Another message starts at zero independently.
    await consume(Incoming(message_id="second-id"))
    assert broker.publisher.default_exchange.publish.call_args.args[0].headers["x-retry-count"] == 1
    assert "x-retry-count" not in original.headers


@pytest.mark.asyncio
async def test_ack_waits_for_publish_confirmation(broker):
    consume, _ = await subscribe(broker)
    pending = asyncio.Event()
    started = asyncio.Event()

    async def publish(*args, **kwargs):
        started.set()
        await pending.wait()

    broker.publisher.default_exchange.publish.side_effect = publish
    message = Incoming()
    task = asyncio.create_task(consume(message))
    await started.wait()
    message.ack.assert_not_awaited()
    pending.set()
    await task
    message.ack.assert_awaited_once()
    kwargs = broker.publisher.default_exchange.publish.call_args.kwargs
    assert kwargs == {"routing_key": "service.events.retry", "mandatory": True, "timeout": 10.0}


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("publish failed"), TimeoutError(),
    aio_pika.exceptions.DeliveryError(None, None),
    aio_pika.exceptions.PublishError(SimpleNamespace(delivery=Basic.Return()), None)])
async def test_failed_or_uncertain_publish_requeues_original(broker, error):
    consume, _ = await subscribe(broker)
    broker.publisher.default_exchange.publish.side_effect = error
    message = Incoming()
    await consume(message)
    message.ack.assert_not_awaited()
    message.reject.assert_not_awaited()
    message.nack.assert_awaited_once_with(requeue=True)
    assert "x-retry-count" not in message.headers


@pytest.mark.asyncio
async def test_closed_delivery_channel_is_left_to_broker_recovery(broker):
    consume, _ = await subscribe(broker)
    message = Incoming()
    message.channel.is_closed = True
    broker.publisher.default_exchange.publish.side_effect = RuntimeError()
    await consume(message)
    message.ack.assert_not_awaited()
    message.nack.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_closing_during_nack_is_left_to_broker_recovery(broker):
    consume, _ = await subscribe(broker)
    message = Incoming()
    message.nack.side_effect = aio_pika.exceptions.ChannelInvalidStateError()
    broker.publisher.default_exchange.publish.side_effect = RuntimeError()
    await consume(message)
    message.ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_logs_omit_payload_and_exception_text(broker, caplog):
    consume, _ = await subscribe(broker)
    message = Incoming(body=b'{"otp":"private-code"}')
    await consume(message)
    assert "private-code" not in caplog.text
    assert "sensitive payload" not in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "message_id='stable-id'" in caplog.text


@pytest.mark.asyncio
async def test_unavailable_publisher_channel_retains_original(broker):
    consume, _ = await subscribe(broker)
    broker.client._publisher_channel_for_use.side_effect = ConnectionError()
    message = Incoming()
    await consume(message)
    message.ack.assert_not_awaited()
    message.nack.assert_awaited_once_with(requeue=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("during_publish", [False, True])
async def test_cancellation_does_not_settle_delivery(broker, during_publish):
    handler = AsyncMock(side_effect=RuntimeError() if during_publish else asyncio.CancelledError())
    consume, _ = await subscribe(broker, handler)
    broker.publisher.default_exchange.publish.side_effect = asyncio.CancelledError()
    message = Incoming()
    with pytest.raises(asyncio.CancelledError):
        await consume(message)
    message.ack.assert_not_awaited()
    message.nack.assert_not_awaited()
    message.reject.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_fails", [False, True])
async def test_ack_error_does_not_trigger_retry_or_nack(broker, handler_fails):
    consume, _ = await subscribe(broker, AsyncMock(side_effect=RuntimeError() if handler_fails else None))
    message = Incoming()
    message.ack.side_effect = RuntimeError("ack failed")
    with pytest.raises(RuntimeError, match="ack failed"):
        await consume(message)
    message.nack.assert_not_awaited()
    assert broker.publisher.default_exchange.publish.await_count == int(handler_fails)


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{"max_retries": 0}, {"requeue_on_error": False}])
async def test_retry_can_be_disabled(broker, options):
    consume, _ = await subscribe(broker, **options)
    message = Incoming()
    await consume(message)
    message.reject.assert_awaited_once_with(requeue=False)
    broker.publisher.default_exchange.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_custom_retry_limit_and_missing_id(broker):
    consume, _ = await subscribe(broker, max_retries=5)
    message = Incoming(headers={"x-retry-count": 4}, message_id=None)
    await consume(message)
    published = broker.publisher.default_exchange.publish.call_args.args[0]
    assert published.message_id
    assert published.headers["x-retry-count"] == 5
    again = Incoming(headers=published.headers, message_id=published.message_id)
    await consume(again)
    again.reject.assert_awaited_once_with(requeue=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [-1, "1", True, 1.5, None, [], {}])
async def test_invalid_retry_count_is_dead_lettered(broker, count):
    consume, handler = await subscribe(broker)
    message = Incoming(headers={"x-retry-count": count})
    await consume(message)
    handler.assert_not_awaited()
    message.reject.assert_awaited_once_with(requeue=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"invalid", b"\xff", b"[]"])
async def test_malformed_payload_is_dead_lettered(broker, body):
    consume, handler = await subscribe(broker)
    message = Incoming(body=body)
    await consume(message)
    handler.assert_not_awaited()
    message.reject.assert_awaited_once_with(requeue=False)


@pytest.mark.asyncio
async def test_invalid_original_routing_header_is_dead_lettered(broker):
    consume, handler = await subscribe(broker)
    message = Incoming(headers={"x-original-routing-key": None})
    await consume(message)
    handler.assert_not_awaited()
    message.reject.assert_awaited_once_with(requeue=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [-1, True, "3", 1.5])
async def test_invalid_retry_limit_fails_before_channel_creation(broker, limit):
    with pytest.raises(ValueError, match="max_retries"):
        await subscribe(broker, max_retries=limit)
    broker.client._new_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_topology_is_declared_without_changing_source_arguments(broker):
    await subscribe(broker)
    assert set(broker.queues) == {"service.events", "service.events.retry", "service.events.dlq"}
    broker.channel.declare_queue.assert_any_await("service.events", durable=True)
    broker.channel.declare_exchange.assert_any_await(
        "service.events.dlx", aio_pika.ExchangeType.FANOUT, durable=True,
    )
    broker.queues["service.events.dlq"].bind.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_declaration", [False, True])
async def test_declarations_run_together_and_settle_before_cleanup(broker, fail_declaration):
    all_started = asyncio.Event()
    release = asyncio.Event()
    started = []
    finished = []
    declare_queue = broker.channel.declare_queue.side_effect
    exchange = broker.channel.declare_exchange.return_value

    async def declare(name, *, is_queue, **kwargs):
        started.append(name)
        if len(started) == 5:
            all_started.set()
        if fail_declaration and name == "events":
            raise RuntimeError("declaration failed")
        await release.wait()
        result = await declare_queue(name, **kwargs) if is_queue else exchange
        finished.append(name)
        return result

    async def declare_exchange(name, *args, **kwargs):
        return await declare(name, is_queue=False, **kwargs)

    async def declare_source_queue(name, **kwargs):
        return await declare(name, is_queue=True, **kwargs)

    broker.channel.declare_exchange.side_effect = declare_exchange
    broker.channel.declare_queue.side_effect = declare_source_queue
    task = asyncio.create_task(subscribe(broker))
    try:
        await asyncio.wait_for(all_started.wait(), timeout=1)
        assert not task.done()
        broker.channel.close.assert_not_awaited()
    finally:
        release.set()
        if fail_declaration:
            with pytest.raises(RuntimeError, match="declaration failed"):
                await task
        else:
            await task
    assert len(finished) == (4 if fail_declaration else 5)
    if fail_declaration:
        broker.channel.close.assert_awaited_once()
        broker.queues["service.events"].consume.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["declare_exchange", "declare_queue"])
async def test_setup_failure_closes_channel(broker, stage):
    getattr(broker.channel, stage).side_effect = RuntimeError("setup failed")
    with pytest.raises(RuntimeError, match="setup failed"):
        await subscribe(broker)
    broker.channel.close.assert_awaited_once()
    assert broker.client._subscriptions == []


@pytest.mark.asyncio
async def test_consumer_registration_failure_closes_channel(broker):
    # Declaration creates queues before consume is called.
    await subscribe(broker)
    broker.queues["service.events"].consume.side_effect = RuntimeError("registration failed")
    with pytest.raises(RuntimeError, match="registration failed"):
        await subscribe(broker)
    broker.channel.close.assert_awaited_once()
    assert len(broker.client._subscriptions) == 1


@pytest.mark.asyncio
async def test_new_channels_enable_confirms_and_raise_returns():
    channel = SimpleNamespace(set_qos=AsyncMock(), close=AsyncMock())
    client = RabbitMQClient()
    client.connect = AsyncMock()
    client._connection = SimpleNamespace(channel=AsyncMock(return_value=channel))
    assert await client._new_channel(prefetch_count=1) is channel
    client._connection.channel.assert_awaited_once_with(publisher_confirms=True, on_return_raises=True)
    channel.set_qos.assert_awaited_once_with(prefetch_count=1)
    channel.set_qos.side_effect = RuntimeError("qos failed")
    with pytest.raises(RuntimeError):
        await client._new_channel(prefetch_count=1)
    channel.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_publish_sets_initial_count_and_timeout(broker):
    exchange = SimpleNamespace(publish=AsyncMock())
    broker.client._declare_exchange = AsyncMock(return_value=exchange)
    message_id = await broker.client.publish("events", "event.created", {"id": 1})
    published = exchange.publish.call_args.args[0]
    assert published.message_id == message_id
    assert published.headers["x-retry-count"] == 0
    assert exchange.publish.call_args.kwargs["timeout"] == 10.0


@pytest.mark.asyncio
async def test_topic_forwards_retry_options():
    client = SimpleNamespace(subscribe=AsyncMock())
    await Topic(client, "events").subscribe("queue", "event.#", AsyncMock(), max_retries=2, requeue_on_error=False)
    assert client.subscribe.call_args.kwargs["max_retries"] == 2
    assert client.subscribe.call_args.kwargs["requeue_on_error"] is False


@pytest.mark.parametrize("overrides", [{"RABBITMQ_MAX_RETRIES": -1}, {"RABBITMQ_PUBLISH_TIMEOUT": 0}])
def test_settings_reject_invalid_limits(overrides):
    with pytest.raises(ValidationError):
        Settings(**overrides)


def test_broker_policy_definitions_cover_all_consumers():
    from app.external.queues.topics.add_group_member import AddGroupMemberTopic
    from app.external.queues.topics.robot_status import RobotStatusTopic
    from app.external.queues.topics.user_verification import UserVerificationTopic
    import re

    definitions = json.loads((Path(__file__).parents[2] / "rabbitmq/policies.json").read_text())
    policies = definitions["policies"]
    assert len(policies) == 6
    for topic in (AddGroupMemberTopic, RobotStatusTopic, UserVerificationTopic):
        queue = topic.queue_name
        source = [p for p in policies if re.search(p["pattern"], queue)]
        retry = [p for p in policies if re.search(p["pattern"], queue + ".retry")]
        assert len(source) == len(retry) == 1
        assert source[0]["definition"] == {"dead-letter-exchange": queue + ".dlx"}
        assert retry[0]["definition"] == {
            "dead-letter-exchange": "", "dead-letter-routing-key": queue, "message-ttl": 5000,
        }
        assert not any(re.search(p["pattern"], queue + ".dlq") for p in policies)
    assert settings.RABBITMQ_MAX_RETRIES == 3
