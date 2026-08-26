from uuid import UUID, uuid4

from fastapi import Depends, Path, Query

from app.common.context import AppContext
from app.common.enum.context_actions import CREATE_BOT, DELETE_BOT, LIST_BOTS
from app.common.exceptions.decorator import exception_handler
from app.common.middleware.auth_middleware import AuthMiddleware
from app.common.middleware.logger import Logger
from app.common.schemas.bot import BotCreateDTO, BotInfo, BotListInfo, BotListQuery
from app.common.schemas.common import BaseResponse, PaginationBaseResponse
from app.common.schemas.user import Credential
from app.services.bot import BotService

logger = Logger()


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

    @exception_handler
    async def list_bots(
        self,
        query: BotListQuery = Query(),
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> PaginationBaseResponse[BotListInfo]:
        ctx = AppContext(trace_id=uuid4(), action=LIST_BOTS, actor=credential.id)
        logger.info(query.tag_ids)
        bots, total = await self.service.list_bots(
            query=query,
            group_id=query.group_id,
            credential=credential,
            ctx=ctx,
        )
        return PaginationBaseResponse[BotListInfo](
            total=total,
            page=query.page,
            page_size=query.page_size,
            items=bots,
        )

    @exception_handler
    async def delete_bot(
        self,
        bot_id: UUID = Path(..., description="Bot id"),
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> None:
        ctx = AppContext(trace_id=uuid4(), action=DELETE_BOT, actor=credential.id)
        await self.service.delete_bot(
            bot_id=bot_id,
            credential=credential,
            ctx=ctx,
            group_id=credential.active_group_id,
        )
