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
    ingestion = RobotStatusMqttIngestion(topic)

    await ingestion.handle_message(f"pleco/robots/{robot_id}/status", payload(robot_id))
    await ingestion.handle_message(f"pleco/robots/{robot_id}/status", payload(uuid4()))

    assert len(topic.events) == 1
    assert topic.events[0].robot_id == robot_id
    assert topic.events[0].type == "robot.connection.changed"
