from uuid import UUID, uuid4

from fastapi import Depends

from app.common.context import AppContext
from app.common.enum.context_actions import LIST_BOTS, SEND_ROBOT_COMMAND
from app.common.exceptions.decorator import exception_handler
from app.common.middleware.auth_middleware import AuthMiddleware
from app.common.schemas.common import BaseResponse
from app.common.schemas.robot_action import RobotCommandInfo, RobotCommandRequest, RobotPosition
from app.common.schemas.user import Credential
from app.services.robot_action import RobotActionService


class RobotActionHandler:
    def __init__(self, robot_action_service: RobotActionService) -> None:
        self.robot_action_service = robot_action_service

    @exception_handler
    async def submit(
        self, bot_id: UUID, payload: RobotCommandRequest,
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> BaseResponse[RobotCommandInfo]:
        result = await self.robot_action_service.submit(
            robot_id=bot_id, request=payload, credential=credential,
            group_id=credential.active_group_id,
            ctx=AppContext(trace_id=uuid4(), action=SEND_ROBOT_COMMAND, actor=credential.id),
        )
        return BaseResponse(data=result, message="Robot command queued", statusCode=202)

    @exception_handler
    async def get_command(
        self, bot_id: UUID, command_id: UUID,
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> BaseResponse[RobotCommandInfo]:
        result = await self.robot_action_service.get_command(
            robot_id=bot_id, command_id=command_id, credential=credential,
            group_id=credential.active_group_id,
            ctx=AppContext(trace_id=uuid4(), action=LIST_BOTS, actor=credential.id),
        )
        return BaseResponse(data=result, message="Success", statusCode=200)

    @exception_handler
    async def get_position(
        self, bot_id: UUID,
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> BaseResponse[RobotPosition | None]:
        result = await self.robot_action_service.get_position(
            robot_id=bot_id, credential=credential, group_id=credential.active_group_id,
            ctx=AppContext(trace_id=uuid4(), action=LIST_BOTS, actor=credential.id),
        )
        return BaseResponse(data=result, message="Success", statusCode=200)
