from uuid import UUID, uuid4

from fastapi import Depends, Query

from app.common.context import AppContext
from app.common.enum.context_actions import LIST_TAGS
from app.common.exceptions.decorator import exception_handler
from app.common.middleware.auth_middleware import AuthMiddleware
from app.common.schemas.common import BaseResponse
from app.common.schemas.tags import TagListInfo
from app.common.schemas.user import Credential
from app.services.tag import TagService


class TagHandler:
    def __init__(self, service: TagService) -> None:
        self.service = service

    @exception_handler
    async def list_tags(
        self,
        group_id: UUID = Query(..., description="Group that owns the tags"),
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> BaseResponse[list[TagListInfo]]:
        ctx = AppContext(trace_id=uuid4(), action=LIST_TAGS, actor=credential.id)
        tags = await self.service.list_tags(
            group_id=group_id, credential=credential, ctx=ctx
        )
        return BaseResponse[list[TagListInfo]](
            data=tags,
            message="Tags retrieved",
            statusCode=200,
        )
