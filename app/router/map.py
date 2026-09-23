from fastapi import APIRouter, status

from app.common.schemas.common import BaseResponse, PaginationBaseResponse
from app.common.schemas.map import (
    MapDetailInfo,
    MapInfo,
    MapListInfo,
)
from app.handler.map import MapHandler


class MapRouter:
    def __init__(self, handler: MapHandler) -> None:
        self.router = APIRouter(prefix="", tags=["Maps"])
        self.handler = handler
        self.router.add_api_route(
            path="/layouts",
            endpoint=self.handler.save_layout,
            methods=["POST"],
            status_code=status.HTTP_204_NO_CONTENT,
            summary="Save a map layout",
            description=(
                "Owners and Admins may atomically save any supplied boundary, "
                "environment-zone, and docking-station sections for an active-group "
                "map. Omitted or null sections are unchanged; empty zone or station "
                "lists clear that section. Changes are applied in boundary, zone, "
                "then station order and roll back together on failure."
            ),
            responses={
                400: {
                    "description": (
                        "Invalid geometry, containment, overlap, dimensions, or "
                        "robot-to-map assignment"
                    )
                },
                401: {"description": "Authentication required"},
                403: {"description": "Active-group Owner or Admin permission required"},
                404: {"description": "Map or robot not found in the active group"},
                409: {
                    "description": (
                        "Zone or station changed or deleted, or robot assignment "
                        "conflict"
                    )
                },
                422: {"description": "Invalid request fields or geometry structure"},
            },
        )
        self.router.add_api_route(
            path="/{map_id}",
            endpoint=self.handler.get_map_detail,
            methods=["GET"],
            response_model=BaseResponse[MapDetailInfo],
            status_code=status.HTTP_200_OK,
            summary="Get active-group map detail",
            description=(
                "Return an active-group map's metadata, compact tags, assigned robots, "
                "boundary, environment zones, and docking stations."
            ),
            responses={
                401: {"description": "Authentication required"},
                403: {"description": "An active group must be selected"},
                404: {"description": "Map not found in the active group"},
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
