from uuid import uuid4

from fastapi import Depends

from app.common.context import AppContext
from app.common.enum.context_actions import CREATE_BOT
from app.common.exceptions.decorator import exception_handler
from app.common.middleware.auth_middleware import AuthMiddleware
from app.common.schemas.bot import BotCreateDTO, BotInfo
from app.common.schemas.common import BaseResponse
from app.common.schemas.user import Credential
from app.services.bot import BotService


class BotHandler:
    def __init__(self, service: BotService) -> None:
        self.service = service

    @exception_handler
    async def create_bot(
        self,
        bot_create: BotCreateDTO,
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> BaseResponse[BotInfo]:
        ctx = AppContext(trace_id=uuid4(), action=CREATE_BOT, actor=credential.id)
        bot = await self.service.create_bot(
            bot_create=bot_create,
            group_id=bot_create.group_id,
            credential=credential,
            ctx=ctx,
        )
        return BaseResponse[BotInfo](
            data=bot,
            message="Bot created",
            statusCode=201,
        )
