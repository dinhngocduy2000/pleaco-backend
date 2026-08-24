"""Mosquitto subscriber that validates and normalizes robot status messages."""

import asyncio
import json
from uuid import UUID

import paho.mqtt.client as mqtt
from pydantic import ValidationError

from app.common.middleware.logger import Logger
from app.common.schemas.robot_status import MqttRobotStatusMessage, RobotStatusEvent
from app.core.config import settings

logger = Logger()


class RobotStatusMqttIngestion:
    topic_filter = "pleco/robots/+/status"

    def __init__(self, robot_status_topic) -> None:
        self._robot_status_topic = robot_status_topic
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="pleaco-backend")
        if settings.MQTT_USERNAME:
            self._client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD or None)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            self._client.connect_async(settings.MQTT_HOST, settings.MQTT_PORT, keepalive=30)
            self._client.loop_start()
        except Exception:
            logger.exception(msg="Unable to start MQTT robot-status ingestion")

    async def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def _on_connect(self, _client, _userdata, _flags, reason_code, _properties) -> None:
        if reason_code != 0:
            logger.error(msg=f"MQTT robot-status connection failed: {reason_code}")
            return
        _client.subscribe(self.topic_filter, qos=1)
        logger.info(msg=f"Subscribed to MQTT robot status topic '{self.topic_filter}'")

    def _on_message(self, _client, _userdata, message) -> None:
        if self._loop is None:
            return
        future = asyncio.run_coroutine_threadsafe(
            self.handle_message(message.topic, message.payload), self._loop
        )
        future.add_done_callback(self._log_failure)

    @staticmethod
    def _log_failure(future) -> None:
        try:
            future.result()
        except Exception:
            logger.exception(msg="MQTT robot-status message processing failed")

    async def handle_message(self, topic: str, raw_payload: bytes) -> None:
        topic_robot_id = self._robot_id_from_topic(topic)
        if topic_robot_id is None:
            logger.error(msg=f"Rejecting unexpected MQTT topic '{topic}'")
            return
        try:
            payload = MqttRobotStatusMessage.model_validate(json.loads(raw_payload.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
            logger.error(msg=f"Rejecting invalid MQTT robot status payload: {error}")
            return
        if payload.robot_id != topic_robot_id:
            logger.error(msg="Rejecting MQTT robot status with topic/payload robot_id mismatch")
            return
        event = RobotStatusEvent(
            message_id=payload.message_id,
            type=(
                "robot.connection.changed"
                if payload.event in {"robot.online", "robot.offline"}
                else "robot.status.changed"
            ),
            robot_id=payload.robot_id,
            ip_address=payload.ip_address,
            sequence_number=payload.sequence_number,
            connection_status=payload.data.connection_status,
            operational_status=payload.data.operational_status,
            occurred_at=payload.timestamp,
        )
        await self._robot_status_topic.publish_status(event)
        logger.info(
            msg=(
                f"Forwarded MQTT {payload.event} for robot {payload.robot_id} "
                f"sequence={payload.sequence_number} to RabbitMQ"
            )
        )

    @staticmethod
    def _robot_id_from_topic(topic: str) -> UUID | None:
        parts = topic.split("/")
        if len(parts) != 4 or parts[0] != "pleco" or parts[1] != "robots" or parts[3] != "status":
            return None
        try:
            return UUID(parts[2])
        except ValueError:
            return None
