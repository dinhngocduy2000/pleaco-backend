"""Real-broker tests; run through scripts/test_rabbitmq.py with explicit test URLs."""

import asyncio
import json
import re
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4

import aio_pika
import httpx
import pytest

from app.external.queues.queue import RabbitMQClient

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def broker(request):
    url = request.config.getoption("--rabbitmq-test-url")
    management_url = request.config.getoption("--rabbitmq-test-management-url")
    if not url or not management_url:
        pytest.skip("Requires explicit disposable RabbitMQ AMQP and management URLs")
    parsed = urlsplit(url)
    vhost = unquote(parsed.path[1:]) or "/"
    api_vhost = quote(vhost, safe="")
    queue_name = f"pleaco-dlq-test.{uuid4()}"
    exchange_name = queue_name + ".events"
    client = RabbitMQClient(url=url)
    connection = await aio_pika.connect_robust(url)
    channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
    # This source predates policy installation, like queues on an existing deployment.
    source = await channel.declare_queue(queue_name, durable=True)
    await channel.default_exchange.publish(aio_pika.Message(b'{"kind":"backlog"}'), routing_key=queue_name)
    template = json.loads((Path(__file__).parents[3] / "rabbitmq/policies.json").read_text())["policies"][:2]
    auth = httpx.BasicAuth(unquote(parsed.username or "guest"), unquote(parsed.password or "guest"))
    async with httpx.AsyncClient(base_url=management_url, auth=auth, timeout=10) as api:
        try:
            for index, entry in enumerate(template):
                definition = dict(entry["definition"])
                if index == 0:
                    definition["dead-letter-exchange"] = queue_name + ".dlx"
                else:
                    definition["dead-letter-routing-key"] = queue_name
                pattern = re.escape(queue_name + (".retry" if index else ""))
                response = await api.put(f"/api/policies/{api_vhost}/{queue_name}.{index}", json={
                    "pattern": f"^{pattern}$", "definition": definition,
                    "priority": 10, "apply-to": "queues",
                })
                response.raise_for_status()
            # No recreation: the message published before policy installation survives.
            backlog = await source.get(timeout=5)
            assert json.loads(backlog.body) == {"kind": "backlog"}
            await backlog.ack()
            response = await api.get(f"/api/queues/{api_vhost}/{queue_name}")
            response.raise_for_status()
            assert response.json()["effective_policy_definition"]["dead-letter-exchange"] == queue_name + ".dlx"
            yield SimpleNamespace(client=client, channel=channel, source=source, queue=queue_name,
                                  exchange=exchange_name, url=url)
        finally:
            await client.close()
            for name in (queue_name + ".observer", queue_name + ".retry", queue_name + ".dlq", queue_name):
                response = await api.delete(f"/api/queues/{api_vhost}/{name}")
                assert response.status_code in (204, 404)
            for name in (exchange_name, queue_name + ".dlx"):
                response = await api.delete(f"/api/exchanges/{api_vhost}/{name}")
                assert response.status_code in (204, 404)
            for index in (0, 1):
                response = await api.delete(f"/api/policies/{api_vhost}/{queue_name}.{index}")
                assert response.status_code in (204, 404)
            await connection.close()


async def wait_for_message(queue, timeout=25):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        message = await queue.get(fail=False, timeout=2)
        if message is not None:
            return message
        await asyncio.sleep(0.1)
    pytest.fail("Expected broker message did not arrive")


async def test_delayed_retries_dead_letter_and_allow_healthy_messages(broker):
    attempts, healthy, observed = [], [], []

    async def handler(message):
        if message.payload["kind"] == "healthy":
            healthy.append(monotonic())
            return
        assert message.routing_key == "event.created"
        attempts.append((monotonic(), message.message_id, message.headers.get("x-retry-count", 0)))
        raise RuntimeError("poison")

    await broker.client.subscribe(broker.exchange, broker.queue, "event.#", handler, prefetch_count=1)
    # A second queue on the same topic must not receive retry copies.
    observer = await broker.channel.declare_queue(broker.queue + ".observer", durable=True)
    exchange = await broker.channel.get_exchange(broker.exchange)
    await observer.bind(exchange, routing_key="event.#")
    poison_id = await broker.client.publish(broker.exchange, "event.created", {"kind": "poison"})
    await broker.client.publish(broker.exchange, "event.created", {"kind": "healthy"})
    dlq = await broker.channel.get_queue(broker.queue + ".dlq")
    dead = await wait_for_message(dlq)
    assert [count for _, _, count in attempts] == [0, 1, 2, 3]
    assert all(message_id == poison_id for _, message_id, _ in attempts)
    assert all(later[0] - earlier[0] >= 4.5 for earlier, later in zip(attempts, attempts[1:]))
    assert len(healthy) == 1 and healthy[0] < attempts[1][0]
    assert dead.message_id == poison_id and dead.headers["x-retry-count"] == 3
    assert any(death["reason"] == "rejected" for death in dead.headers["x-death"])
    await dead.ack()
    for _ in range(2):
        message = await wait_for_message(observer, timeout=5)
        observed.append(json.loads(message.body)["kind"])
        await message.ack()
    assert sorted(observed) == ["healthy", "poison"]
    assert await observer.get(fail=False) is None


async def test_malformed_json_is_dead_lettered_without_retry(broker):
    async def handler(message):
        pytest.fail("Malformed JSON reached the handler")

    await broker.client.subscribe(broker.exchange, broker.queue, "event.#", handler)
    exchange = await broker.channel.get_exchange(broker.exchange)
    await exchange.publish(aio_pika.Message(b"not-json", message_id="malformed"), routing_key="event.created")
    dead = await wait_for_message(await broker.channel.get_queue(broker.queue + ".dlq"), timeout=5)
    assert dead.message_id == "malformed"
    assert dead.body == b"not-json"
    assert "x-retry-count" not in dead.headers
    await dead.ack()


async def test_retry_survives_client_disconnect(broker, monkeypatch):
    acknowledged = asyncio.Event()
    original_ack = aio_pika.IncomingMessage.ack

    async def track_ack(message, *args, **kwargs):
        await original_ack(message, *args, **kwargs)
        acknowledged.set()

    monkeypatch.setattr(aio_pika.IncomingMessage, "ack", track_ack)

    async def failing_handler(message):
        raise RuntimeError("retry after disconnect")

    await broker.client.subscribe(broker.exchange, broker.queue, "event.#", failing_handler)
    message_id = await broker.client.publish(broker.exchange, "event.created", {"kind": "poison"})
    # Wait for the original ACK as well as the confirmed retry, avoiding an
    # intentional at-least-once duplicate window during consumer shutdown.
    await asyncio.wait_for(acknowledged.wait(), timeout=5)
    retry = await broker.channel.get_queue(broker.queue + ".retry")
    assert (await retry.declare()).message_count == 1
    await broker.client.close()
    await asyncio.sleep(5.1)
    completed = asyncio.Event()
    received = []

    async def recovered_handler(message):
        received.append(message)
        completed.set()

    await broker.client.subscribe(broker.exchange, broker.queue, "event.#", recovered_handler)
    await asyncio.wait_for(completed.wait(), timeout=10)
    assert received[0].message_id == message_id
    assert received[0].headers["x-retry-count"] == 1
    assert received[0].routing_key == "event.created"
