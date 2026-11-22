"""Authorize map cleaning tasks and prepare their ordered movement commands."""

from decimal import Decimal
from math import dist, isfinite
from uuid import UUID

from app.common.context import AppContext
from app.common.enum.direction import Direction
from app.common.enum.user_roles import GroupRole
from app.common.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
)
from app.common.middleware.logger import Logger
from app.common.schemas.cleaning_task import CleaningMove, CleaningTaskInfo
from app.common.schemas.user import Credential
from app.core.rbac.permissions import PermissionService
from app.core.rbac.role_validation import require_permission
from app.services.cleaning_path import CleaningPathService, PlannedCleaningPath
from app.services.cleaning_path_planner import EPSILON, Point2D
from app.services.robot_action import RobotActionService

logger = Logger()

def _build_cardinal_move(origin: Point2D, target: Point2D, ctx: AppContext | None = None) -> CleaningMove:
    """Convert a route segment to a direction and an exact decimal distance."""
    delta_x = Decimal(str(target[0])) - Decimal(str(origin[0]))
    delta_y = Decimal(str(target[1])) - Decimal(str(origin[1]))
    # Snap the stationary axis to the origin to absorb planner rounding noise.
    if abs(delta_y) < EPSILON and delta_x:
        target = target[0], origin[1]
        direction = Direction.EAST if delta_x > 0 else Direction.WEST
        distance = abs(delta_x)
    elif abs(delta_x) < EPSILON and delta_y:
        target = origin[0], target[1]
        direction = Direction.NORTH if delta_y > 0 else Direction.SOUTH
        distance = abs(delta_y)
    else:
        logger.warning(msg="Cleaning route requires a non-cardinal move", context=ctx)
        raise BadRequestException(message="Cleaning route requires a non-cardinal move")
    return {
        "origin": {"x": origin[0], "y": origin[1]},
        "target": {"x": target[0], "y": target[1]},
        "direction": direction,
        "distance": f"{distance:f}m",
    }


def build_cleaning_moves(path: PlannedCleaningPath, ctx: AppContext | None = None) -> list[CleaningMove]:
    """Follow the planner's ring order and compile its points into commands."""
    logger.debug(msg="Compiling planned cleaning route into movements", context=ctx)
    ring_sections = [
        points
        for name, points in path.sections.items()
        if name.startswith("ring_layer_")
    ]
    center_section = path.sections.get("to_center", [])
    dock_return_section = path.sections.get("return_to_dock", [])
    if not ring_sections or not center_section or not dock_return_section:
        logger.warning(msg="Cleaning route is missing a required section", context=ctx)
        raise BadRequestException(
            message="Cleaning route requires rings, center travel, and dock return"
        )
    if (
        dist(center_section[-1], path.center) >= EPSILON
        or dist(dock_return_section[-1], path.start) >= EPSILON
    ):
        logger.warning(msg="Cleaning route does not reach the center and starting dock", context=ctx)
        raise BadRequestException(
            message="Cleaning route must reach the map center and return to the starting dock"
        )

    moves: list[CleaningMove] = []
    origin = path.start
    for section in (*ring_sections, center_section, dock_return_section):
        if not section or dist(section[0], origin) >= EPSILON:
            logger.warning(msg="Cleaning route sections are disconnected", context=ctx)
            raise BadRequestException(
                message="Cleaning route sections are disconnected"
            )
        for target in section:
            if not all(isfinite(coordinate) for coordinate in target):
                logger.warning(msg="Cleaning route contains a non-finite coordinate", context=ctx)
                raise BadRequestException(
                    message="Cleaning route contains an invalid coordinate"
                )
            if dist(origin, target) < EPSILON:
                continue
            move = _build_cardinal_move(origin, target, ctx)
            moves.append(move)
            # The next command starts where this command will actually end.
            origin = move["target"]["x"], move["target"]["y"]
    if not moves:
        logger.warning(msg="Cleaning route contains no movements", context=ctx)
        raise BadRequestException(
            message="Cleaning route does not contain any movement"
        )
    logger.debug(msg=f"Compiled {len(moves)} cleaning movements", context=ctx)
    return moves


class CleaningTaskService:
    """Authorize starts and scope task progress to the caller's group and map."""

    def __init__(
        self,
        cleaning_path_service: CleaningPathService,
        robot_action_service: RobotActionService,
        permission_service: PermissionService,
    ) -> None:
        self.cleaning_path_service = cleaning_path_service
        self.robot_action_service = robot_action_service
        self.permission_service = permission_service

    @staticmethod
    def _require_active_group(group_id: UUID | None, credential: Credential, ctx: AppContext | None = None) -> UUID:
        """Require the requested group to match the authenticated active group."""
        if group_id is None or group_id != credential.active_group_id:
            logger.warning(msg="Cleaning task requires a matching active group", context=ctx)
            raise ForbiddenException(message="An active group must be selected")
        logger.debug(msg="Validated active group for cleaning task", context=ctx)
        return group_id

    @require_permission(GroupRole.MEMBER)
    async def start(
        self,
        *,
        map_id: UUID,
        robot_id: UUID,
        group_id: UUID | None,
        credential: Credential,
        ctx: AppContext,
    ) -> CleaningTaskInfo:
        """Check robot readiness, build the route, and reserve the first move."""
        logger.info(msg=f"Starting cleaning task for robot {robot_id} on map {map_id}", context=ctx)
        try:
            active_group_id = self._require_active_group(group_id, credential, ctx)
        except ForbiddenException:
            logger.warning(msg="Cleaning task start requires the active group", context=ctx)
            raise
        await self.robot_action_service.ensure_cleaning_task_can_start(
            robot_id=robot_id,
            map_id=map_id,
            group_id=active_group_id,
            actor_id=credential.id,
            ctx=ctx,
        )
        path = await self.cleaning_path_service.plan_for_robot(
            map_id=map_id,
            robot_id=robot_id,
            group_id=active_group_id,
            actor_id=credential.id,
            ctx=ctx,
            offline_status_code=409,
            for_execution=True,
        )
        try:
            moves = build_cleaning_moves(path, ctx)
        except BadRequestException:
            logger.exception(msg=f"Invalid cleaning route for robot {robot_id} on map {map_id}", context=ctx)
            raise
        task = await self.robot_action_service.start_cleaning_task(
            robot_id=robot_id,
            map_id=map_id,
            group_id=active_group_id,
            actor_id=credential.id,
            layout_signature=path.layout_signature,
            moves=moves,
            start_position=path.start_position,
            confirmed_peer_ids=path.confirmed_peer_ids,
            ctx=ctx,
        )
        logger.info(msg=f"Cleaning task {task.task_id} queued for robot {robot_id}", context=ctx)
        return task

    @require_permission(GroupRole.MEMBER)
    async def get(
        self,
        *,
        map_id: UUID,
        task_id: UUID,
        group_id: UUID | None,
        credential: Credential,
        ctx: AppContext,
    ) -> CleaningTaskInfo:
        """Read a task only when it belongs to the requested map and active group."""
        logger.debug(msg=f"Reading cleaning task {task_id} for map {map_id}", context=ctx)
        try:
            active_group_id = self._require_active_group(group_id, credential, ctx)
        except ForbiddenException:
            logger.warning(msg=f"Cleaning task {task_id} lookup requires the active group", context=ctx)
            raise
        task = await self.robot_action_service.get_cleaning_task(task_id)
        if task is None or task.group_id != active_group_id or task.map_id != map_id:
            logger.warning(msg=f"Cleaning task {task_id} unavailable for map {map_id}", context=ctx)
            raise NotFoundException(message="Cleaning task not found")
        logger.debug(msg=f"Cleaning task {task_id} status is {task.status.value}", context=ctx)
        return task
