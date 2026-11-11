from fastapi import APIRouter

from app.common.schemas.cleaning_path import CleaningPathPreviewData
from app.common.schemas.common import BaseResponse
from app.handler.cleaning_path import CleaningPathHandler


class CleaningPathRouter:
    def __init__(self, handler: CleaningPathHandler) -> None:
        # Mount the preview as a read-only planning operation beneath a map.
        self.router = APIRouter(tags=["Maps"])
        self.router.add_api_route(
            "/{map_id}/cleaning-path/preview", handler.preview, methods=["POST"],
            response_model=BaseResponse[CleaningPathPreviewData],
            summary="Preview a complete cleaning path for a robot on this map",
        )
