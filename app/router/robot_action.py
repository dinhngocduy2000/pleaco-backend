from fastapi import APIRouter, status

from app.common.schemas.common import BaseResponse
from app.common.schemas.robot_action import RobotCommandInfo, RobotPosition
from app.handler.robot_action import RobotActionHandler


class RobotActionRouter:
    def __init__(self, robot_action_handler: RobotActionHandler) -> None:
        self.router = APIRouter()
        self.router.add_api_route(
            "/{bot_id}/commands", robot_action_handler.submit, methods=["POST"],
            response_model=BaseResponse[RobotCommandInfo], status_code=status.HTTP_202_ACCEPTED,
            summary="Queue a cleaning movement for an online robot",
        )
        self.router.add_api_route(
            "/{bot_id}/commands/loop", robot_action_handler.submit_loop, methods=["POST"],
            response_model=BaseResponse[RobotCommandInfo], status_code=status.HTTP_202_ACCEPTED,
            summary="Queue a four-action cleaning loop for an online robot",
        )
        self.router.add_api_route(
            "/{bot_id}/commands/{command_id}", robot_action_handler.get_command, methods=["GET"],
            response_model=BaseResponse[RobotCommandInfo], summary="Read robot command status",
        )
        self.router.add_api_route(
            "/{bot_id}/position", robot_action_handler.get_position, methods=["GET"],
            response_model=BaseResponse[RobotPosition | None], summary="Read last confirmed robot position",
        )
