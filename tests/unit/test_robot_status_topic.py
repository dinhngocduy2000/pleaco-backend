from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.external.queues.queue import TopicMessage
from app.external.queues.topics.robot_status import RobotStatusTopic


class FakeClient:
    def __init__(self):
        self.published = []
        self.subscribed = None
        self.unbound = None

    async def publish(self, **kwargs):
        self.published.append(kwargs)
        return "message-id"

    async def subscribe(self, **kwargs):
        self.subscribed = kwargs

        async def declare_exchange(*args, **kwargs):
            return "exchange"

        async def unbind(exchange, routing_key):
            self.unbound = (exchange, routing_key)

        return SimpleNamespace(
            channel=SimpleNamespace(declare_exchange=declare_exchange),
            queue=SimpleNamespace(unbind=unbind),
        )


class FakeService:
    def __init__(self):
        self.events = []

    async def process_status_event(self, event):
        self.events.append(event)


@pytest.mark.asyncio
async def test_queue_binds_only_connectivity_and_ignores_old_backlog():
    client = FakeClient()
    service = FakeService()
    topic = RobotStatusTopic(client, service)
    await topic.start_consumer()

    assert client.subscribed["routing_key"] == "robot.connection.changed"
    assert client.unbound == ("exchange", "robot.#")
    await topic._handle_message(TopicMessage(payload={}, routing_key="robot.heartbeat"))
    await topic._handle_message(TopicMessage(payload={}, routing_key="robot.status.changed"))
    assert service.events == []

    robot_id = uuid4()
    await topic._handle_message(TopicMessage(
        payload={
            "message_id": str(uuid4()), "type": "robot.connection.changed",
            "robot_id": str(robot_id), "sequence_number": 2,
            "connection_status": "ONLINE", "occurred_at": "2026-10-15T00:00:00Z",
        },
        routing_key="robot.connection.changed",
    ))
    assert service.events[0].robot_id == robot_id
