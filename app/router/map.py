from fastapi import APIRouter, status

from app.common.schemas.common import BaseResponse, PaginationBaseResponse
from app.common.schemas.map import MapBoundaryInfo, MapInfo, MapListInfo
from app.handler.map import MapHandler


class MapRouter:
    def __init__(self, handler: MapHandler) -> None:
        self.router = APIRouter(prefix="", tags=["Maps"])
        self.handler = handler
        self.router.add_api_route(
            path="/boundary",
            endpoint=self.handler.save_boundary,
            methods=["POST"],
            response_model=BaseResponse[MapBoundaryInfo],
            status_code=status.HTTP_200_OK,
            summary="Create or replace a map boundary",
            description=(
                "Owners and Admins may save boundaries for active-group maps. "
                "DIMENSIONS generates a rectangle; CUSTOM and TEACH_MODE require "
                "a valid local X/Y polygon within the map dimensions."
            ),
            responses={
                400: {"description": "Invalid dimensions, polygon topology, or containment"},
                401: {"description": "Authentication required"},
                403: {"description": "Active-group Owner or Admin permission required"},
                404: {"description": "Map not found in the active group"},
                422: {"description": "Invalid request fields or geometry structure"},
            },
        )
        self.router.add_api_route(
            path="",
            endpoint=self.handler.list_maps,
            methods=["GET"],
            response_model=PaginationBaseResponse[MapListInfo],
            status_code=status.HTTP_200_OK,
            summary="List active-group maps",
            description=(
                "List maps in the caller's active group with their boundary geometry. "
                "Maps without a boundary return geometry as null."
            ),
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
