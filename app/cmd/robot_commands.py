import asyncio

from fastapi import FastAPI
from loguru import logger

from app.core.config import settings
from app.external.mqtt.robot_action import RobotActionMqttGateway
from app.services.robot_action import RobotActionService

ROBOT_COMMAND_STARTUP_RETRY_SECONDS = 5.0


async def start_robot_commands(
    application: FastAPI,
    robot_action_service: RobotActionService,
    robot_action_mqtt_gateway: RobotActionMqttGateway,
) -> None:
    """Restore command transports without gating the HTTP application.

    Shared topic subscriptions cache successful registrations. A partially
    failed pass retries only the missing consumer.
    """
    while True:
        try:
            async with asyncio.timeout(max(
                settings.RABBITMQ_CONNECT_TIMEOUT, settings.RABBITMQ_PUBLISH_TIMEOUT
            )):
                await robot_action_mqtt_gateway.start()
                await robot_action_service.start()
        except Exception:
            logger.warning("Robot command transports unavailable; startup will retry")
            await asyncio.sleep(ROBOT_COMMAND_STARTUP_RETRY_SECONDS)
        else:
            application.state.robot_command_reconciler_task = asyncio.create_task(
                robot_action_service.run_reconciler(), name="robot-command-reconciler"
            )
            return
