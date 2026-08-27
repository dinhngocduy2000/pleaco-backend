from uuid import uuid4

from fastapi import Depends

from app.common.context import AppContext
from app.common.enum.context_actions import CREATE_MAP
from app.common.exceptions.decorator import exception_handler
from app.common.middleware.auth_middleware import AuthMiddleware
from app.common.schemas.common import BaseResponse
from app.common.schemas.map import MapCreateDTO, MapInfo
from app.common.schemas.user import Credential
from app.services.map import MapService


class MapHandler:
    def __init__(self, service: MapService) -> None:
        self.service = service

    @exception_handler
    async def create_map(
        self,
        map_create: MapCreateDTO,
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> BaseResponse[MapInfo]:
        ctx = AppContext(trace_id=uuid4(), action=CREATE_MAP, actor=credential.id)
        map_info = await self.service.create_map(
            map_create=map_create,
            group_id=map_create.group_id,
            credential=credential,
            ctx=ctx,
        )
        return BaseResponse[MapInfo](
            data=map_info,
            message="Map created",
            statusCode=201,
        )
