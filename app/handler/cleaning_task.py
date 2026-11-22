"""HTTP entry points for starting cleaning tasks and reading progress."""

from uuid import UUID, uuid4

from fastapi import Depends

from app.common.context import AppContext
from app.common.enum.context_actions import LIST_MAPS, SEND_ROBOT_COMMAND
from app.common.exceptions.decorator import exception_handler
from app.common.middleware.auth_middleware import AuthMiddleware
from app.common.middleware.logger import Logger
from app.common.schemas.cleaning_task import CleaningTaskInfo, CleaningTaskStartRequest
from app.common.schemas.common import BaseResponse
from app.common.schemas.user import Credential
from app.services.cleaning_task import CleaningTaskService

logger = Logger()

class CleaningTaskHandler:
    """Pass authenticated request context to the cleaning task service."""

    def __init__(self, service: CleaningTaskService) -> None:
        self.service = service

    @exception_handler
    async def start(
        self, map_id: UUID, payload: CleaningTaskStartRequest,
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> BaseResponse[CleaningTaskInfo]:
        """Return the queued task and its ID after accepting the start request."""
        ctx = AppContext(trace_id=uuid4(), action=SEND_ROBOT_COMMAND, actor=credential.id)
        logger.info(msg=f"Cleaning task requested for robot {payload.robot_id} on map {map_id}", context=ctx)
        try:
            task = await self.service.start(
                map_id=map_id, robot_id=payload.robot_id, group_id=credential.active_group_id,
                credential=credential, ctx=ctx,
            )
        except Exception:
            logger.exception(msg=f"Cleaning task start failed for robot {payload.robot_id} on map {map_id}", context=ctx)
            raise
        logger.info(msg=f"Cleaning task {task.task_id} accepted", context=ctx)
        return BaseResponse(data=task, message="Cleaning task queued", statusCode=202)

    @exception_handler
    async def get(
        self, map_id: UUID, task_id: UUID,
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> BaseResponse[CleaningTaskInfo]:
        """Return the latest task progress for the requested map."""
        ctx = AppContext(trace_id=uuid4(), action=LIST_MAPS, actor=credential.id)
        logger.debug(msg=f"Reading cleaning task {task_id} on map {map_id}", context=ctx)
        try:
            task = await self.service.get(
                map_id=map_id, task_id=task_id, group_id=credential.active_group_id,
                credential=credential, ctx=ctx,
            )
        except Exception:
            logger.exception(msg=f"Cleaning task {task_id} lookup failed for map {map_id}", context=ctx)
            raise
        logger.debug(msg=f"Returned cleaning task {task_id}", context=ctx)
        return BaseResponse(data=task, message="Success", statusCode=200)
