import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.external.mqtt.robot_status import RobotStatusMqttIngestion


class FakeTopic:
    def __init__(self):
        self.events = []

    async def publish_status(self, event):
        self.events.append(event)



class FakeService:
    def __init__(self):
        self.heartbeats = []
        self.statuses = []

    async def process_heartbeat_event(self, event):
        self.heartbeats.append(event)

    async def process_status_event(self, event):
        self.statuses.append(event)


def payload(robot_id, *, message_id=None, sequence_number=1):
    return json.dumps(
        {
            "message_id": str(message_id or uuid4()),
            "robot_id": str(robot_id),
            "ip_address": "192.168.10.24",
            "event": "robot.online",
            "sequence_number": sequence_number,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": {"connection_status": "ONLINE"},
        }
    ).encode()


@pytest.mark.asyncio
async def test_normalizes_only_topic_matched_robot_messages():
    robot_id = uuid4()
    topic = FakeTopic()
    service = FakeService()
    ingestion = RobotStatusMqttIngestion(topic, service)
    payload_with_group = json.loads(payload(robot_id).decode())
    payload_with_group["group_id"] = str(uuid4())

    await ingestion.handle_message(f"pleco/robots/{robot_id}/status", payload(robot_id))
    await ingestion.handle_message(f"pleco/robots/{robot_id}/status", payload(uuid4()))
    await ingestion.handle_message(
        f"pleco/robots/{robot_id}/status",
        json.dumps(payload_with_group).encode(),
    )

    assert len(topic.events) == 1
    assert topic.events[0].robot_id == robot_id
    assert topic.events[0].type == "robot.connection.changed"
    assert service.statuses == []


@pytest.mark.asyncio
async def test_validates_and_routes_heartbeat_without_status_sequence():
    robot_id = uuid4()
    message_id = uuid4()
    topic = FakeTopic()
    service = FakeService()
    ingestion = RobotStatusMqttIngestion(topic, service)
    heartbeat = {
        "message_id": str(message_id),
        "robot_id": str(robot_id),
        "event": "robot.heartbeat",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    await ingestion.handle_message(
        f"pleco/robots/{robot_id}/status", json.dumps(heartbeat).encode()
    )
    heartbeat["robot_id"] = str(uuid4())
    await ingestion.handle_message(
        f"pleco/robots/{robot_id}/status", json.dumps(heartbeat).encode()
    )
    heartbeat["robot_id"] = str(robot_id)
    heartbeat["unexpected"] = "rejected"
    await ingestion.handle_message(
        f"pleco/robots/{robot_id}/status", json.dumps(heartbeat).encode()
    )

    assert len(service.heartbeats) == 1
    assert service.heartbeats[0].robot_id == robot_id
    assert service.heartbeats[0].message_id == message_id
    assert topic.events == []


@pytest.mark.asyncio
async def test_operational_update_bypasses_rabbitmq_and_cannot_change_connectivity():
    robot_id = uuid4()
    topic = FakeTopic()
    service = FakeService()
    ingestion = RobotStatusMqttIngestion(topic, service)
    updated = json.loads(payload(robot_id).decode())
    updated["event"] = "robot.status.updated"
    updated["data"] = {"operational_status": "EXECUTING"}

    await ingestion.handle_message(f"pleco/robots/{robot_id}/status", json.dumps(updated).encode())
    assert len(service.statuses) == 1
    assert service.statuses[0].type == "robot.status.changed"
    assert topic.events == []

    updated["data"] = {}
    await ingestion.handle_message(f"pleco/robots/{robot_id}/status", json.dumps(updated).encode())
    assert len(service.statuses) == 1
