from fastapi import APIRouter, status

from app.common.schemas.common import BaseResponse
from app.common.schemas.tags import TagListInfo
from app.handler.tag import TagHandler


class TagRouter:
    def __init__(self, handler: TagHandler) -> None:
        self.router = APIRouter(prefix="", tags=["Tags"])
        self.handler = handler
        self.router.add_api_route(
            path="",
            endpoint=self.handler.list_tags,
            methods=["GET"],
            response_model=BaseResponse[list[TagListInfo]],
            status_code=status.HTTP_200_OK,
            summary="List group tags",
            description="List tags for a group the caller is an accepted member of.",
        )
