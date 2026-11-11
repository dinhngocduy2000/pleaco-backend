from uuid import UUID, uuid4

from fastapi import Depends

from app.common.context import AppContext
from app.common.enum.context_actions import PREVIEW_CLEANING_PATH
from app.common.exceptions.decorator import exception_handler
from app.common.middleware.auth_middleware import AuthMiddleware
from app.common.middleware.logger import Logger
from app.common.schemas.cleaning_path import (
    CleaningPathPoint, CleaningPathPreviewData, CleaningPathPreviewRequest,
    CleaningPathRingLayer,
)
from app.common.schemas.common import BaseResponse
from app.common.schemas.user import Credential
from app.services.cleaning_path import CleaningPathService

logger = Logger()


class CleaningPathHandler:
    """Translate the HTTP request and service route into the API response."""

    def __init__(self, service: CleaningPathService) -> None:
        self.service = service

    @exception_handler
    async def preview(self, map_id: UUID, body: CleaningPathPreviewRequest,
                      credential: Credential = Depends(AuthMiddleware.auth_middleware)) -> BaseResponse[CleaningPathPreviewData]:
        # 1. Create one trace context for this request and pass it through all
        # preview layers so logs can be joined by trace ID.
        ctx = AppContext(trace_id=uuid4(), action=PREVIEW_CLEANING_PATH, actor=credential.id)
        logger.info(msg=f"Cleaning path preview requested for map {map_id}, robot {body.robot_id}", context=ctx)
        # 2. Delegate authorization, layout/position validation, and planning.
        # Re-log unexpected failures with the same context before propagation.
        try:
            sections = await self.service.preview(
                map_id=map_id, robot_id=body.robot_id, group_id=credential.active_group_id,
                credential=credential, ctx=ctx,
            )
        except Exception:
            logger.error(msg="Cleaning path preview request failed", context=ctx)
            raise
        # 3. Put variable ring count inside a fixed API object while preserving
        # ring order and each section's coordinate order.
        logger.info(msg=f"Cleaning path preview returned {len(sections)} route sections", context=ctx)
        def points_for(name: str) -> list[CleaningPathPoint]:
            return [CleaningPathPoint(x=x, y=y) for x, y in sections[name]]

        data = CleaningPathPreviewData(
            ring_layers=[CleaningPathRingLayer(name=name, points=points_for(name))
                         for name in sections if name.startswith("ring_layer_")],
            to_center=points_for("to_center"),
            return_to_dock=points_for("return_to_dock"),
        )
        return BaseResponse(data=data, message="Cleaning path preview generated", statusCode=200)
