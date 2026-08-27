from fastapi import APIRouter, status

from app.common.schemas.common import BaseResponse, PaginationBaseResponse
from app.common.schemas.map import MapInfo, MapListInfo
from app.handler.map import MapHandler


class MapRouter:
    def __init__(self, handler: MapHandler) -> None:
        self.router = APIRouter(prefix="", tags=["Maps"])
        self.handler = handler
        self.router.add_api_route(
            path="",
            endpoint=self.handler.list_maps,
            methods=["GET"],
            response_model=PaginationBaseResponse[MapListInfo],
            status_code=status.HTTP_200_OK,
            summary="List active-group maps",
            description="List maps in the caller's active group.",
        )
        self.router.add_api_route(
            path="",
            endpoint=self.handler.create_map,
            methods=["POST"],
            response_model=BaseResponse[MapInfo],
            status_code=status.HTTP_201_CREATED,
            summary="Create a map",
            description="Create a map and optionally assign unassigned group robots and tags.",
        )
