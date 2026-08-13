import json

import pytest
from pydantic import BaseModel

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
