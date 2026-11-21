"""Routes for map-scoped cleaning tasks."""

from fastapi import APIRouter, status

from app.common.schemas.cleaning_task import CleaningTaskInfo
from app.common.schemas.common import BaseResponse
from app.handler.cleaning_task import CleaningTaskHandler


class CleaningTaskRouter:
    """Register the start and progress endpoints beneath the maps prefix."""

    def __init__(self, handler: CleaningTaskHandler) -> None:
        self.router = APIRouter(tags=["Maps"])
        self.router.add_api_route(
            "/{map_id}/cleaning-tasks", handler.start, methods=["POST"],
            response_model=BaseResponse[CleaningTaskInfo], status_code=status.HTTP_202_ACCEPTED,
            summary="Start a cleaning task for a robot assigned to the map",
        )
        self.router.add_api_route(
            "/{map_id}/cleaning-tasks/{task_id}", handler.get, methods=["GET"],
            response_model=BaseResponse[CleaningTaskInfo], summary="Read cleaning task progress",
        )
