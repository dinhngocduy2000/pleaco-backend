"""MQTT command gateway and validated robot ACK ingress."""

import asyncio
import json
import socket
import threading
import time
from concurrent.futures import Future
from datetime import datetime, timezone
from typing import cast
from uuid import UUID, uuid4

import paho.mqtt.client as mqtt
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode
from pydantic import ValidationError

from app.common.middleware.logger import Logger
from app.common.schemas.robot_action import RobotAcknowledgement, RobotCommand
from app.core.config import settings
from app.external.queues.topics.robot_action import RobotActionTopic
from app.external.redis.robot_commands import RobotCommandStore

logger = Logger()


class _MqttConnectError(ConnectionError):
    """A connection failed before any movement publication was attempted."""


class RobotActionMqttGateway:
    topic_filter = "pleco/robots/+/acks"

    def __init__(
        self,
        robot_action_topic: RobotActionTopic,
        robot_command_store: RobotCommandStore,
    ) -> None:
        self.robot_action_topic = robot_action_topic
        self.robot_command_store = robot_command_store
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False
        self._stopping = False
        self._publications: set[asyncio.Task[None]] = set()
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"pleaco-actions-{uuid4()}", manual_ack=True
        )
        if settings.MQTT_USERNAME:
            self._client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD or None)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    async def start(self) -> None:
        if self._started:
            return
        self._stopping = False
        self._loop = asyncio.get_running_loop()
        try:
            self._client.connect_async(settings.MQTT_HOST, settings.MQTT_PORT, keepalive=30)
            if self._client.loop_start() != mqtt.MQTT_ERR_SUCCESS:
                raise ConnectionError("Unable to start MQTT acknowledgement subscriber")
        except BaseException:
            self._client.disconnect()
            await asyncio.to_thread(self._client.loop_stop)
            raise
        self._started = True

    async def stop(self) -> None:
        self._stopping = True
        publications = tuple(task for task in self._publications if task is not asyncio.current_task())
        for task in publications:
            task.cancel()
        if publications:
            await asyncio.gather(*publications, return_exceptions=True)
        self._client.disconnect()
        await asyncio.to_thread(self._client.loop_stop)
        self._started = False

    def _on_connect(
        self, client: mqtt.Client, _userdata: object, _flags: mqtt.ConnectFlags,
        reason_code: ReasonCode, _properties: Properties | None,
    ) -> None:
        if reason_code == 0:
            client.subscribe(self.topic_filter, qos=1)

    def _on_message(self, client: mqtt.Client, _userdata: object, message: mqtt.MQTTMessage) -> None:
        if self._loop is None:
            return
        future = asyncio.run_coroutine_threadsafe(self._receive_message(client, message), self._loop)
        future.add_done_callback(self._log_failure)

    async def _receive_message(self, client: mqtt.Client, message: mqtt.MQTTMessage) -> None:
        await self.handle_message(message.topic, message.payload)
        # Settle MQTT only after durable RabbitMQ publication (or rejection).
        client.ack(message.mid, message.qos)

    @staticmethod
    def _log_failure(future: Future[None]) -> None:
        try:
            future.result()
        except Exception:
            logger.warning(msg="Robot ACK ingress failed")

    async def handle_message(self, topic: str, raw_payload: bytes) -> None:
        try:
            parts = topic.split("/")
            if len(parts) != 4 or parts[:2] != ["pleco", "robots"] or parts[3] != "acks":
                return
            ack = RobotAcknowledgement.model_validate(json.loads(raw_payload.decode("utf-8")))
            if ack.robot_id != UUID(parts[2]):
                return
        except (UnicodeDecodeError, ValueError, ValidationError):
            logger.warning(msg="Ignoring invalid MQTT robot acknowledgement")
            return
        # Do not let arbitrary external UUIDs create queues or channels.
        record = await self.robot_command_store.active(ack.robot_id)
        if record is None or record["command_id"] != str(ack.command_id):
            return
        await self.robot_action_topic.publish_ack(ack)

    def _new_publisher(self) -> mqtt.Client:
        # The acknowledgement subscriber must never retain movement messages.
        # Manual loop() does not reconnect, including initial connection failure.
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"pleaco-dispatch-{uuid4()}",
            clean_session=True,
            reconnect_on_failure=False,
        )
        if settings.MQTT_USERNAME:
            client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD or None)
        return client

    @staticmethod
    def _publish_once(
        client: mqtt.Client,
        command: RobotCommand,
        stop_event: threading.Event,
        expires: float,
        host: str,
        port: int,
        connect_expires: float | None = None,
    ) -> None:
        """Own the one-shot client entirely in one bounded, joined worker.

        Never start Paho's background reconnect loop. A failed QoS1 message may
        remain in this retired client's memory, but it has no socket, loop, or
        reuse path that could send it later.
        """
        def remaining(until: float | None = None) -> float:
            if stop_event.is_set():
                raise InterruptedError("MQTT command publication cancelled")
            budget = min(expires, until if until is not None else expires) - time.monotonic()
            if budget <= 0:
                raise TimeoutError("MQTT command publication deadline elapsed")
            return budget

        def poll(until: float | None = None) -> None:
            result = client.loop(timeout=min(0.05, remaining(until)))
            if result != mqtt.MQTT_ERR_SUCCESS:
                raise ConnectionError("MQTT command connection failed")

        try:
            try:
                client.connect_timeout = remaining(connect_expires)
                if client.connect(host, port, keepalive=30) != mqtt.MQTT_ERR_SUCCESS:
                    raise ConnectionError("MQTT command connection failed")
                while not client.is_connected():
                    poll(connect_expires)
            except OSError as error:
                raise _MqttConnectError("MQTT command connection failed") from error
            remaining()
            info = client.publish(
                f"pleco/robots/{command.robot_id}/commands",
                command.model_dump_json(), qos=1, retain=False,
            )
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise ConnectionError("MQTT command publication failed")
            while not info.is_published():
                poll()
            remaining()
        finally:
            # Close first: disconnect() itself can flush queued packets. After
            # expiry/cancellation cleanup must not flush a buffered movement.
            try:
                sock = client.socket()
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    finally:
                        sock.close()
            finally:
                try:
                    client.disconnect()
                finally:
                    client.loop_stop()

    async def publish_command(self, command: RobotCommand, *, deadline: datetime | None = None) -> None:
        if self._stopping:
            raise ConnectionError("MQTT command gateway is stopping")
        if not self._client.is_connected():
            raise ConnectionError("MQTT acknowledgement subscriber is disconnected")
        budget = settings.RABBITMQ_PUBLISH_TIMEOUT
        if deadline is not None:
            budget = min(budget, (deadline - datetime.now(timezone.utc)).total_seconds())
        if budget <= 0:
            raise TimeoutError("MQTT command publication deadline elapsed")
        expires = time.monotonic() + budget
        task = cast(asyncio.Task[None], asyncio.current_task())
        self._publications.add(task)
        stop_event = threading.Event()
        worker: asyncio.Task[None] | None = None
        try:
            # DNS may block independently of a socket connect timeout. Resolve
            # before creating a publisher; abandoned DNS work cannot publish.
            addresses = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(
                    settings.MQTT_HOST, settings.MQTT_PORT, type=socket.SOCK_STREAM
                ), timeout=budget,
            )
            if not addresses:
                raise ConnectionError("MQTT command hostname has no addresses")
            for index, address in enumerate(addresses):
                host, port = address[4][:2]
                client = self._new_publisher()
                connect_expires = time.monotonic() + max(0, expires - time.monotonic()) / (len(addresses) - index)
                worker = asyncio.create_task(asyncio.to_thread(
                    self._publish_once, client, command, stop_event, expires, host, port, connect_expires
                ))
                try:
                    await asyncio.shield(worker)
                except _MqttConnectError:
                    if index == len(addresses) - 1:
                        raise
                    # Before publish only: preserve hostname address fallback
                    # without ever reusing a client that retained a movement.
                    continue
                return
        except BaseException:
            stop_event.set()
            if worker is not None:
                # asyncio cancellation does not stop a worker thread. Join it
                # before propagating cancellation, including repeated cancels.
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if worker.done() and not worker.cancelled():
                    worker.exception()
            raise
        finally:
            self._publications.discard(task)
