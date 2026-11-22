"""Robot action lifecycle, authorization, and confirmed-position reconciliation."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from math import isfinite
from typing import TypeGuard
from uuid import UUID, uuid4

from shapely.geometry import LineString, Point, shape
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.context import AppContext
from app.common.enum.robot import RobotConnectionStatus
from app.common.enum.user_roles import GroupRole
from app.common.exceptions import BadRequestException, ForbiddenException, NotFoundException
from app.common.middleware.logger import Logger
from app.common.schemas.cleaning_task import CleaningMove, CleaningTaskInfo, CleaningTaskRecord, CleaningTaskStatus
from app.common.schemas.robot_action import (
    CommandStatus, RobotAcknowledgement, RobotCommand, RobotCommandInfo, RobotPosition,
    RobotActionContext, RobotCommandRecord, RobotCommandRequest, RobotPositionRecord,
    RobotCoordinates, RobotMovementCoordinates, RobotLoopRequest, CleaningAction,
)
from app.common.utils.robot_movement import RectangleBoundary, select_movement
from app.common.schemas.user import Credential
from app.common.utils.robot_actions import apply_robot_ack, dispatch_robot_command, to_robot_command, validate_active_command
from app.core.config import settings
from app.core.rbac.permissions import PermissionService
from app.core.rbac.role_validation import require_permission
from app.external.mqtt.robot_action import RobotActionMqttGateway
from app.external.queues.topics.robot_action import RobotActionTopic
from app.external.realtime.robot_action import RobotActionRealtimePublisher
from app.external.redis.robot_commands import RobotCommandStore
from app.repository.cleaning_path import CleaningPathRepository
from app.repository.registry import DbAccessContext, Registry
from app.services.cleaning_path import layout_signature

logger = Logger()


@dataclass(frozen=True)
class LoopActionResult:
    action: CleaningAction
    target_x: Decimal
    target_y: Decimal
    no_movement: bool


class RobotActionService:
    """Coordinate authorized commands, execution results, and confirmed positions."""

    def __init__(
        self,
        registry: Registry,
        permission_service: PermissionService,
        robot_command_store: RobotCommandStore,
        robot_action_topic: RobotActionTopic,
        robot_action_mqtt_gateway: RobotActionMqttGateway,
        *,
        clock: Callable[[], datetime] | None = None,
        cleaning_path_repository: CleaningPathRepository | None = None,
        realtime_publisher: RobotActionRealtimePublisher | None = None,
    ) -> None:
        """Bind the dependencies and register the queued-message handler.

        Args:
            registry: Registry providing robot queries and database transactions.
            permission_service: Active-group membership and role checker.
            robot_command_store: Atomic Redis command and confirmed-position storage.
            robot_action_topic: RabbitMQ publisher and per-robot consumer manager.
            robot_action_mqtt_gateway: Device-facing command publisher.
            clock: Callable returning a timezone-aware current datetime.
                Defaults to the current UTC time; injectable for deterministic
                deadline and heartbeat checks.
        """
        self.registry = registry
        self.permission_service = permission_service
        self.robot_command_store = robot_command_store
        self.robot_action_topic = robot_action_topic
        self.robot_action_mqtt_gateway = robot_action_mqtt_gateway
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.cleaning_path_repository = cleaning_path_repository
        self.realtime_publisher = realtime_publisher
        self.robot_action_topic.set_handlers(self.process_command, self._process_movement_ack)

    def _command_deadline(self) -> str:
        """Return a new ISO 8601 deadline using the configured command timeout."""
        return (self.clock() + timedelta(seconds=settings.ROBOT_COMMAND_TIMEOUT_SECONDS)).isoformat()

    async def ensure_cleaning_task_can_start(
        self, *, robot_id: UUID, map_id: UUID, group_id: UUID, actor_id: UUID, ctx: AppContext | None = None,
    ) -> None:
        """Reject starts when the robot, rollout flag, or command transport is unavailable."""
        logger.debug(msg=f"Checking cleaning readiness for robot {robot_id} on map {map_id}", context=ctx)
        if not settings.CLEANING_TASKS_ENABLED:
            logger.warning(msg=f"Cleaning task admission disabled for robot {robot_id}", context=ctx)
            raise BadRequestException(message="Cleaning task admission is disabled", status_code=503)
        context = await self._get_robot_context(robot_id, group_id, actor_id)
        if context is None or context["group_id"] != group_id or context["map_id"] != map_id:
            logger.warning(msg=f"Robot {robot_id} is unavailable on map {map_id}", context=ctx)
            raise NotFoundException(message="Robot is not included in this map")
        try:
            await self._ensure_robot_ready(robot_id=robot_id, context=context)
        except Exception:
            logger.exception(msg=f"Robot {robot_id} failed cleaning readiness checks", context=ctx)
            raise
        if settings.ROBOT_COMMANDS_PAUSED or not self.robot_action_topic.ready or not self.robot_action_mqtt_gateway.ready:
            logger.warning(msg=f"Command transport unavailable for robot {robot_id}", context=ctx)
            raise BadRequestException(message="Robot command transport is unavailable or paused", status_code=503)
        if await self.robot_command_store.active(robot_id) is not None:
            logger.warning(msg=f"Robot {robot_id} already has active work", context=ctx)
            raise BadRequestException(message="Robot is busy", status_code=409)
        logger.debug(msg=f"Robot {robot_id} is ready for a cleaning task", context=ctx)

    async def start_cleaning_task(
        self, *, robot_id: UUID, map_id: UUID, group_id: UUID, actor_id: UUID,
        layout_signature: str, moves: list[CleaningMove], start_position: RobotPositionRecord | None,
        confirmed_peer_ids: list[str] | None = None,
        ctx: AppContext | None = None,
    ) -> CleaningTaskInfo:
        """Reserve the robot against its planned start position and queue the first move."""
        logger.debug(msg=f"Reserving cleaning task for robot {robot_id} with {len(moves)} moves", context=ctx)
        # Planning may take time, so check readiness again before reserving the robot.
        await self.ensure_cleaning_task_can_start(robot_id=robot_id, map_id=map_id, group_id=group_id, actor_id=actor_id, ctx=ctx)
        # A task reservation needs a first movement to queue.
        if not moves:
            logger.warning(msg=f"Cleaning route has no moves for robot {robot_id}", context=ctx)
            raise BadRequestException(message="Cleaning route has no moves")
        # A saved position must match both the selected robot and the route's first waypoint.
        if start_position is not None and (
            start_position.get("robot_id") != str(robot_id)
            or start_position.get("group_id") != str(group_id)
            or start_position.get("map_id") != str(map_id)
            or any(moves[0]["origin"][axis] != start_position.get(axis) for axis in ("x", "y"))
        ):
            raise BadRequestException(message="Cleaning route start does not match the saved position", status_code=409)
        now = self.clock()
        task: CleaningTaskRecord = {
            "task_id": str(uuid4()), "robot_id": str(robot_id), "map_id": str(map_id),
            "group_id": str(group_id), "layout_signature": layout_signature,
            "status": CleaningTaskStatus.QUEUED.value, "total_moves": len(moves),
            "completed_moves": 0, "next_waypoint": moves[0]["target"], "next_check_at": now.isoformat(),
            "occupancy_retries_used": 0, "terminal_reason": None,
            "confirmed_peer_ids": confirmed_peer_ids or [],
        }
        if ctx is not None:
            task["trace_id"] = str(ctx.trace_id)
            if ctx.actor:
                task["actor_id"] = str(ctx.actor)
        command = self._build_cleaning_command(
            task, moves[0], now,
            origin_verified=start_position is not None and start_position.get("verified") is True,
        )
        # Reserve only while the planning snapshot still matches and the robot has no pending movement.
        if not await self.robot_command_store.reserve_task(command, start_position, task, moves):
            logger.warning(msg=f"Cleaning task reservation failed for robot {robot_id}", context=ctx)
            raise BadRequestException(message="Robot is busy, its position changed, or its movement is unconfirmed", status_code=409)
        self._log_cleaning_task(task)
        try:
            await self.robot_action_topic.publish_command(to_robot_command(command))
            logger.info(msg=f"Published first command for cleaning task {task['task_id']}", context=ctx)
        except Exception:
            # The reconciler can publish the command from the saved reservation.
            logger.exception(msg=f"Cleaning task {task['task_id']} awaits broker publication", context=ctx)
        return CleaningTaskInfo.model_validate(task)

    async def get_cleaning_task(self, task_id: UUID) -> CleaningTaskInfo | None:
        """Read the task snapshot for the caller to check against its map and group."""
        task = await self.robot_command_store.get_task(task_id)
        return CleaningTaskInfo.model_validate(task) if task else None

    def _build_cleaning_command(
        self, task: CleaningTaskRecord, move: CleaningMove, dispatch_at: datetime, *, origin_verified: bool,
    ) -> RobotCommandRecord:
        """Give a planned move its own command ID, dispatch window, and route flags."""
        action = CleaningAction(
            direction=move["direction"], distance=move["distance"],
            is_start=task["completed_moves"] == 0,
            is_finish=task["completed_moves"] + 1 == task["total_moves"],
        )
        command = RobotCommand(robot_id=UUID(task["robot_id"]), command_id=uuid4(), attempt=1, action=action)
        now = self.clock().isoformat()
        return {
            **command.model_dump(mode="json"), "task_id": task["task_id"],
            "origin_verified": origin_verified,
            "group_id": task["group_id"], "map_id": task["map_id"],
            "origin": move["origin"], "target": move["target"], "status": CommandStatus.QUEUED.value,
            "created_at": now, "updated_at": now, "failure_reason": None,
            "not_before": dispatch_at.isoformat(),
            "deadline": (dispatch_at + timedelta(seconds=settings.ROBOT_COMMAND_TIMEOUT_SECONDS)).isoformat(),
        }

    @staticmethod
    def _log_cleaning_task(task: CleaningTaskRecord) -> None:
        """Log committed task progress with identifiers for correlating robot events."""
        ctx = AppContext(
            trace_id=UUID(task.get("trace_id", task["task_id"])),
            action="cleaning_task", actor=UUID(task["actor_id"]) if task.get("actor_id") else None,
        )
        logger.info(msg=(f"Cleaning task {task['task_id']} for robot {task['robot_id']} "
                         f"status={task['status']} completed_moves={task['completed_moves']} "
                         f"occupancy_retries={task['occupancy_retries_used']} reason={task['terminal_reason']}"), context=ctx)

    async def _publish_loop_command(self, command: RobotCommand) -> None:
        await self.robot_action_topic.publish_command(command)
        logger.info(msg=f"Robot loop payload attempt={command.attempt} payload={command.model_dump_json()}")

    async def _get_robot_context(
        self, robot_id: UUID, group_id: UUID | None = None, actor_id: UUID | None = None
    ) -> RobotActionContext | None:
        """Load persisted robot, map, and docking facts in a short transaction.

        Args:
            robot_id (UUID): Canonical robot identifier.

        Returns:
            dict | None: Ownership, connectivity, map dimensions, and docking
            centroid, or None when the robot does not exist.
        """
        if group_id is None:
            group_id = await self.registry.resolve_robot_group(robot_id)
        if group_id is None:
            return None
        return await self.registry.transaction_wrapper(
            lambda session: self.registry.robot_action_repo().get_context(session, robot_id),
            DbAccessContext(actor_id=actor_id, group_id=group_id),
        )

    async def _get_group_robot_context(
        self, robot_id: UUID, credential: Credential, group_id: UUID | None,
    ) -> RobotActionContext:
        """Load a robot only within the caller's selected active group.

        Args:
            robot_id (UUID): Robot whose ownership must be checked.
            credential (Credential): Authenticated caller and active-group claims.
            group_id (UUID | None): Requested group, which must match the claims.

        Returns:
            dict: Persisted robot context belonging to the active group.

        Raises:
            ForbiddenException: If the requested group is absent or does not
                match the caller's active group.
            NotFoundException: If the robot is missing or belongs to another
                group; ownership is not disclosed across groups.
        """
        if group_id is None or group_id != credential.active_group_id:
            raise ForbiddenException(message="An active group must be selected")
        context = await self._get_robot_context(
            robot_id, group_id=group_id, actor_id=credential.id
        )
        if context is None or str(context["group_id"]) != str(group_id):
            raise NotFoundException(message="Robot not found")
        return context

    async def _is_robot_online(self, context: RobotActionContext) -> bool:
        """Require persisted ONLINE status and a fresh backend heartbeat.

        Args:
            context: Persisted robot context containing its ID and connectivity.

        Returns:
            bool: True when the cached heartbeat is timezone-aware and its age
            falls between zero and the configured heartbeat TTL. Offline robots
            and missing, malformed, future, or stale timestamps return False.

        Redis access errors propagate so callers can fail closed rather than
        treating unavailable liveness data as proof that the robot is online.
        """
        if context["connection_status"] != RobotConnectionStatus.ONLINE:
            return False
        heartbeat = await self.robot_command_store.redis_client.get(f"robot:{context['robot_id']}:last_seen_at")
        try:
            last_seen = datetime.fromisoformat(heartbeat) if heartbeat else None
            heartbeat_is_fresh = (
                last_seen is not None
                and last_seen.tzinfo is not None
                and 0 <= (self.clock() - last_seen).total_seconds() <= settings.HEARTBEAT_REDIS_TTL_SECONDS
            )
            return heartbeat_is_fresh
        except (ValueError, TypeError):
            return False

    async def _ensure_robot_ready(
        self, *, robot_id: UUID, context: RobotActionContext,
    ) -> None:
        """Check command prerequisites and reconcile the robot's pending work.

        Args:
            robot_id: Robot whose pending command should be reconciled.
            context: Persisted robot connectivity and assigned-map facts.

        Raises:
            BadRequestException: With status 503 when liveness cannot be
                checked, 409 when the robot is offline or stale, or 400 when
                the robot has no assigned map.

        Once the prerequisites pass, reconciliation may expire, cancel, or
        recover an existing pending command before a new submission proceeds.
        """
        try:
            online = await self._is_robot_online(context)
        except Exception as error:
            raise BadRequestException(message="Robot liveness is unavailable", status_code=503) from error
        if not online:
            raise BadRequestException(message="Robot is offline or stale", status_code=409)
        if context["map_id"] is None or context["dimension_x"] is None or context["dimension_y"] is None:
            raise BadRequestException(message="Robot must have an assigned map")
        await self.reconcile_robot(robot_id)

    @staticmethod
    def _resolve_origin(
        *,
        position: RobotPositionRecord | None,
        context: RobotActionContext,
        group_id: UUID | None,
    ) -> tuple[Decimal, Decimal]:
        """Resolve the movement origin from a matching position or assigned dock.

        Args:
            position: Last confirmed position, if one is cached.
            context: Current robot map and docking-station centroid.
            group_id: Active group whose cached position may be used.

        Returns:
            tuple[Decimal, Decimal]: Origin X/Y coordinates in meters. A cached
            position must match the active group and current map; otherwise,
            use the assigned docking station's centroid.

        Raises:
            BadRequestException: If neither a matching position nor a docking
                centroid is available.
        """
        position_matches_map = position and position.get("group_id") == str(group_id) and position.get("map_id") == str(context["map_id"])
        if position_matches_map:
            return Decimal(str(position["x"])), Decimal(str(position["y"]))
        if context["dock_x"] is not None and context["dock_y"] is not None:
            return Decimal(str(context["dock_x"])), Decimal(str(context["dock_y"]))
        raise BadRequestException(message="Robot has no confirmed position or assigned docking station")

    def _validate_movement(
        self,
        *,
        context: RobotActionContext,
        coordinates: RobotMovementCoordinates,
    ) -> None:
        """Validate a proposed movement against the map's dimensions.

        Args:
            context: Robot context containing the assigned map's dimensions.
            coordinates: Current origin and proposed target coordinates in
                meters, retaining Decimal precision during validation.

        Raises:
            BadRequestException: If the origin is nonfinite or the target is
                outside the map dimensions. Targets exactly on an edge are
                allowed.
        """
        if not (
            coordinates["origin_x"].is_finite()
            and coordinates["origin_y"].is_finite()
            and 0 <= coordinates["target_x"] <= context["dimension_x"]
            and 0 <= coordinates["target_y"] <= context["dimension_y"]
        ):
            raise BadRequestException(message="Movement target is outside map dimensions")

    @require_permission(GroupRole.MEMBER)
    async def submit(
        self, *, robot_id: UUID, request: RobotCommandRequest,
        credential: Credential, group_id: UUID | None, ctx: AppContext,
    ) -> RobotCommandInfo:
        """Authorize, reserve, and queue one cleaning movement for a live robot.

        Resolve the origin from a matching cached position or the assigned dock
        centroid, then validate the target against map dimensions. Atomically
        reserve the robot and save dispatch intent before publication. A failed
        initial command publication leaves that intent available for recovery;
        position is written only after a successful execution acknowledgement.

        Args:
            robot_id (UUID): Robot to command within the active group.
            request (RobotCommandRequest): Validated cleaning action and distance.
            credential (Credential): Authenticated caller; MEMBER or above is
                required by the permission decorator.
            group_id (UUID | None): Caller-selected active group.
            ctx (AppContext): Actor and tracing context used for authorization.

        Returns:
            RobotCommandInfo: The queued command snapshot, including its UUID,
            first attempt, action, and dispatch deadline.

        Raises:
            ForbiddenException: If role or active-group requirements fail.
            NotFoundException: If the robot is missing from the active group.
            BadRequestException: With status 409 for offline, stale, or busy
                robots, or a changed position during reservation; 400 for a
                missing map/origin or invalid target; 503 when the initial
                liveness check cannot access its dependencies.
        """
        return await self._submit_command(robot_id, request, credential, group_id, loop=False)

    @require_permission(GroupRole.MEMBER)
    async def submit_loop(
        self, *, robot_id: UUID, request: RobotLoopRequest,
        credential: Credential, group_id: UUID | None, ctx: AppContext,
    ) -> RobotCommandInfo:
        action = CleaningAction(
            direction=request.direction, distance=request.distance,
            is_start=True, is_finish=False,
        )
        return await self._submit_command(
            robot_id, RobotCommandRequest(action=action), credential, group_id, loop=True,
        )

    def generate_loop_action(
        self, action: CleaningAction, context: RobotActionContext,
        origin_x: Decimal, origin_y: Decimal, distance: Decimal,
    ) -> LoopActionResult:
        """Select the first loop step and report when no full step fits."""
        movement = select_movement(
            RectangleBoundary(context["dimension_x"], context["dimension_y"]),
            origin_x, origin_y, distance, action.direction,
            width=context["dimension_x"], height=context["dimension_y"],
        )
        if movement is None:
            return LoopActionResult(action, origin_x, origin_y, True)
        direction, target_x, target_y = movement
        return LoopActionResult(
            action.model_copy(update={"direction": direction}), target_x, target_y, False,
        )

    async def _submit_command(
        self, robot_id: UUID, request: RobotCommandRequest,
        credential: Credential, group_id: UUID | None, *, loop: bool,
    ) -> RobotCommandInfo:
        context = await self._get_group_robot_context(robot_id, credential, group_id)
        await self._ensure_robot_ready(robot_id=robot_id, context=context)
        position = await self.robot_command_store.position(robot_id)
        origin_x, origin_y = self._resolve_origin(
            position=position,
            context=context,
            group_id=group_id,
        )
        distance = Decimal(request.action.distance[:-1])
        no_movement = False
        if loop:
            loop_action = self.generate_loop_action(
                request.action, context, origin_x, origin_y, distance,
            )
            action = loop_action.action
            target_x, target_y = loop_action.target_x, loop_action.target_y
            no_movement = loop_action.no_movement
        else:
            step_x, step_y = {"NORTH": (0, 1), "SOUTH": (0, -1), "EAST": (1, 0), "WEST": (-1, 0)}[request.action.direction]
            target_x, target_y = origin_x + step_x * distance, origin_y + step_y * distance
            action = request.action
        self._validate_movement(
            context=context,
            coordinates=RobotMovementCoordinates(
                origin_x=origin_x,
                origin_y=origin_y,
                target_x=target_x,
                target_y=target_y,
            ),
        )
        if settings.ROBOT_COMMANDS_PAUSED:
            raise BadRequestException(message="Robot commands are temporarily paused", status_code=503)
        if not self.robot_action_topic.ready or not self.robot_action_mqtt_gateway.ready:
            raise BadRequestException(message="Robot command transport is unavailable", status_code=503)
        created_at = self.clock().isoformat()
        command = RobotCommand(robot_id=robot_id, command_id=uuid4(), attempt=1, action=action)
        record: RobotCommandRecord = {
            **command.model_dump(mode="json"), "group_id": str(group_id),
            "map_id": str(context["map_id"]), "origin": {"x": float(origin_x), "y": float(origin_y)},
            "target": {"x": float(target_x), "y": float(target_y)},
            "status": CommandStatus.QUEUED.value, "created_at": created_at, "updated_at": created_at,
            "deadline": self._command_deadline(), "failure_reason": None,
        }
        if loop:
            record["loop"] = True
        if (
            position and position.get("verified") is True and position.get("robot_id") == str(robot_id)
            and position.get("group_id") == str(group_id) and position.get("map_id") == str(context["map_id"])
        ):
            record["origin_verified"] = True
        if not await self.robot_command_store.reserve(record, position):
            raise BadRequestException(message="Robot is busy, its position changed, or its movement is unconfirmed", status_code=409)
        if no_movement:
            await self._finish_command(record, CommandStatus.FAILED, "No movement direction fits map dimensions")
            return self._command_info((await self.robot_command_store.get(robot_id, command.command_id)) or record)
        try:
            if loop:
                await self._publish_loop_command(command)
            else:
                await self.robot_action_topic.publish_command(command)
        except Exception:
            # Persisted QUEUED intent is retried by reconciliation, never lost.
            logger.warning(msg=f"Robot command {command.command_id} awaits broker publication")
        return self._command_info(record)

    @staticmethod
    def _command_info(record: RobotCommandRecord) -> RobotCommandInfo:
        """Project an internal Redis record onto the public command schema.

        Args:
            record: Saved command and internal ownership/navigation metadata.

        Returns:
            RobotCommandInfo: Validated public fields without internal metadata.

        Raises:
            ValidationError: If the selected fields do not form a valid command
                status response.
        """
        public_fields = RobotCommandInfo.model_fields
        return RobotCommandInfo.model_validate({key: value for key, value in record.items() if key in public_fields})

    @require_permission(GroupRole.GUEST)
    async def get_command(
        self, *, robot_id: UUID, command_id: UUID, credential: Credential,
        group_id: UUID | None, ctx: AppContext,
    ) -> RobotCommandInfo:
        """Reconcile active work and read a command from the caller's group.

        Reconciliation can expire or cancel pending work, or republish eligible
        saved dispatch intent, before the requested command record is read.

        Args:
            robot_id (UUID): Robot currently belonging to the active group.
            command_id (UUID): Command whose retained status is requested.
            credential (Credential): Authenticated caller with GUEST or above.
            group_id (UUID | None): Caller-selected active group.
            ctx (AppContext): Actor and tracing context for authorization.

        Returns:
            RobotCommandInfo: Public status retained in Redis for this group.

        Raises:
            ForbiddenException: If role or active-group requirements fail.
            NotFoundException: If the robot or command is missing, the command
                has expired from Redis, or either belongs to another group.
        """
        await self._get_group_robot_context(robot_id, credential, group_id)
        await self.reconcile_robot(robot_id)
        record = await self.robot_command_store.get(robot_id, command_id)
        if record is None or record["group_id"] != str(group_id):
            raise NotFoundException(message="Command not found")
        return self._command_info(record)

    @require_permission(GroupRole.GUEST)
    async def get_position(
        self, *, robot_id: UUID, credential: Credential,
        group_id: UUID | None, ctx: AppContext,
    ) -> RobotPosition | None:
        """Read the last confirmed position on the robot's current group/map.

        Args:
            robot_id (UUID): Robot currently belonging to the active group.
            credential (Credential): Authenticated caller with GUEST or above.
            group_id (UUID | None): Caller-selected active group.
            ctx (AppContext): Actor and tracing context for authorization.

        Returns:
            RobotPosition | None: The cached position, or None if it is absent
            or belongs to a previous group/map. This read neither falls back to
            the docking station nor refreshes the position TTL.

        Raises:
            ForbiddenException: If role or active-group requirements fail.
            NotFoundException: If the robot is missing from the active group.
        """
        context = await self._get_group_robot_context(robot_id, credential, group_id)
        position = await self.robot_command_store.position(robot_id)
        if position is None or position.get("group_id") != str(group_id) or position.get("map_id") != str(context["map_id"]):
            return None
        return RobotPosition.model_validate(position)

    async def _finish_command(
        self, record: RobotCommandRecord, status: CommandStatus, reason: str | None = None, *,
        position: RobotPositionRecord | None = None,
    ) -> bool:
        """Atomically finalize a command and release its pending reservation.

        Args:
            record: Expected current command snapshot for compare-and-swap.
            status: Terminal lifecycle state to save.
            reason (str | None): Optional failure or cancellation explanation.
            position (dict | None): Confirmed coordinates to write atomically
                with successful completion and the position's 12-hour TTL.

        Returns:
            bool: True if the transition was applied; False if the expected
            reservation, record, or applicable deadline check no longer matches.
            Interrupted cleaning tasks clear their cached position. Other
            transitions without a position preserve the existing cache entry.
        """
        updated_record: RobotCommandRecord = {**record, "status": status.value, "updated_at": self.clock().isoformat(), "failure_reason": reason}
        if task_id := record.get("task_id"):
            task = await self.robot_command_store.get_task(task_id)
            if task is not None:
                return await self._transition_cleaning_task(
                    record, updated_record, task, status=CleaningTaskStatus(status.value), reason=reason,
                    terminal=True, position=position,
                )
        changed = await self.robot_command_store.transition(record, updated_record, terminal=True, position=position)
        if (
            changed and position is not None and self.realtime_publisher is not None
            and (not record.get("task_id") or status == CommandStatus.SUCCEEDED)
        ):
            await self.realtime_publisher.publish(position)
        return changed

    async def _transition_cleaning_task(
        self, command: RobotCommandRecord, updated_command: RobotCommandRecord, task: CleaningTaskRecord, *,
        status: CleaningTaskStatus, reason: str | None = None, terminal: bool = False,
        position: RobotPositionRecord | None = None, next_command: RobotCommandRecord | None = None,
        guard: dict[str, str | None] | None = None, retries: int | None = None,
        confirmed_peers: set[str] | None = None,
    ) -> bool:
        """Keep task progress, the active command, and any confirmed position in sync."""
        active_move = next_command or updated_command
        has_scheduled_check = not terminal and status != CleaningTaskStatus.AWAITING_ACK
        updated_task: CleaningTaskRecord = {
            **task, "status": status.value, "terminal_reason": reason,
            "completed_moves": task["completed_moves"] + int(position is not None),
            "next_waypoint": None if terminal else active_move["target"],
            "next_check_at": active_move.get("not_before") if has_scheduled_check else None,
        }
        if retries is not None:
            updated_task["occupancy_retries_used"] = retries
        if confirmed_peers is not None:
            updated_task["confirmed_peer_ids"] = sorted(confirmed_peers)
        changed = await self.robot_command_store.transition_task(
            command, updated_command, task, updated_task,
            terminal=terminal, position=position, next_command=next_command, guard=guard,
        )
        if changed:
            self._log_cleaning_task(updated_task)
            if (
                position is not None and self.realtime_publisher is not None
                and (not terminal or status == CleaningTaskStatus.SUCCEEDED)
            ):
                await self.realtime_publisher.publish(position)
        else:
            logger.warning(
                msg=f"Cleaning task {task['task_id']} transition to {status.value} was not applied",
                context=AppContext(
                    trace_id=UUID(task.get("trace_id", task["task_id"])), action="cleaning_task",
                    actor=UUID(task["actor_id"]) if task.get("actor_id") else None,
                ),
            )
        return changed

    async def _can_process_command(self, record: RobotCommandRecord) -> bool:
        """Check an active command's deadline, ownership/map, and robot liveness.

        Args:
            record: Active command snapshot containing its original group/map
                identity and the current attempt's deadline.

        Returns:
            bool: True while all checks pass. Otherwise, attempt an atomic
            TIMED_OUT or CANCELLED transition and return False. Position is
            preserved. Dependency failures propagate to the calling boundary.
        """
        if self.clock() >= datetime.fromisoformat(record["deadline"]):
            await self._finish_command(record, CommandStatus.TIMED_OUT, "Acknowledgement or dispatch timeout")
            return False
        context = await self._get_robot_context(UUID(record["robot_id"]))
        if context is None or str(context["group_id"]) != record["group_id"] or str(context["map_id"]) != record["map_id"]:
            await self._finish_command(record, CommandStatus.CANCELLED, "Robot ownership or map changed")
            return False
        if not await self._is_robot_online(context):
            await self._finish_command(record, CommandStatus.CANCELLED, "Robot is offline or stale")
            return False
        return True

    async def _validate_active_command(self, message: RobotCommand) -> RobotCommandRecord | None:
        """Match an active command and enforce its deadline, ownership, and liveness.

        An ineligible command may be atomically timed out or cancelled.
        """
        return await validate_active_command(message, self.robot_command_store, self._can_process_command)

    async def process_message(self, message: RobotCommand) -> None:
        """Compatibility entry point for command and movement ACK callers."""
        if isinstance(message, RobotAcknowledgement):
            await self._process_movement_ack(message)
        else:
            await self.process_command(message)

    async def _process_movement_ack(self, message: RobotAcknowledgement) -> None:
        command = await self._validate_active_command(message)
        if command is None:
            if message.ack:
                await self._reconcile_late_position(message)
            return
        if command.get("task_id"):
            await self._process_cleaning_ack(message, command)
            return
        if command.get("loop"):
            await self._process_loop_ack(message, command)
            return
        await self.process_ack(message, retry_enabled=True, on_success_ack=self._confirmed_position)

    async def _reconcile_late_position(self, message: RobotAcknowledgement) -> None:
        """Accept exact execution evidence for a previously verified movement."""
        movement = await self.robot_command_store.unconfirmed_movement(message.robot_id)
        if (
            movement is None or movement.get("origin_verified") is not True
            or any(movement.get(field) != value for field, value in message.model_dump(mode="json", exclude={"ack"}).items())
        ):
            return
        context = await self._get_robot_context(message.robot_id)
        if context is None or (
            str(context["group_id"]) != movement["group_id"] or str(context["map_id"]) != movement["map_id"]
        ):
            return
        position = self._confirmed_position(movement)
        if await self.robot_command_store.confirm_unconfirmed_movement(movement, position):
            if self.realtime_publisher is not None:
                await self.realtime_publisher.publish(position)

    async def _process_cleaning_ack(self, message: RobotAcknowledgement, command: RobotCommandRecord) -> None:
        """Confirm a move and either schedule its successor or finish the task."""
        if command["status"] != CommandStatus.AWAITING_ACK.value:
            return
        if not message.ack:
            await self._finish_command(command, CommandStatus.FAILED, "Robot action rejected")
            return
        position = self._confirmed_position(command)
        task = await self.robot_command_store.get_task(command["task_id"])
        if task is None:
            await self._finish_command(command, CommandStatus.FAILED, "Cleaning task state unavailable", position=position)
            return
        completed_command: RobotCommandRecord = {
            **command, "status": CommandStatus.SUCCEEDED.value, "updated_at": position["updated_at"],
        }
        completed_moves = task["completed_moves"] + 1
        if completed_moves == task["total_moves"]:
            changed = await self._transition_cleaning_task(
                command, completed_command, task, status=CleaningTaskStatus.SUCCEEDED, terminal=True, position=position,
            )
            if not changed and await self.robot_command_store.get_task(command["task_id"]) is None:
                await self._finish_command(command, CommandStatus.FAILED, "Cleaning task state unavailable", position=position)
            return
        next_move = await self.robot_command_store.get_task_move(task["task_id"], completed_moves)
        if next_move is None:
            await self._finish_command(command, CommandStatus.FAILED, "Cleaning route state unavailable", position=position)
            return
        next_dispatch_at = self.clock() + timedelta(seconds=settings.CLEANING_STEP_INTERVAL_SECONDS)
        next_command = self._build_cleaning_command(
            {**task, "completed_moves": completed_moves}, next_move, next_dispatch_at,
            origin_verified=position.get("verified") is True,
        )
        changed = await self._transition_cleaning_task(
            command, completed_command, task, status=CleaningTaskStatus.WAITING_INTERVAL,
            position=position, next_command=next_command, retries=0,
        )
        # A competing ACK may already have advanced the task. Only recover missing state
        # here if this command still holds the reservation.
        if not changed and await self.robot_command_store.active(command["robot_id"]) == command:
            if not await self.robot_command_store.has_task_moves(task["task_id"]) or await self.robot_command_store.get_task(task["task_id"]) is None:
                await self._finish_command(command, CommandStatus.FAILED, "Cleaning route state unavailable", position=position)

    async def _process_loop_ack(self, message: RobotAcknowledgement, command: RobotCommandRecord) -> None:
        if command["status"] != CommandStatus.AWAITING_ACK.value:
            return
        if not message.ack:
            await self._finish_command(command, CommandStatus.FAILED, "Robot action rejected")
            return
        position = self._confirmed_position(command)
        if message.attempt == 4:
            if message.action.is_finish:
                await self._finish_command(command, CommandStatus.SUCCEEDED, position=position)
            return
        context = await self._get_robot_context(message.robot_id)
        if context is None or context["dimension_x"] is None or context["dimension_y"] is None:
            await self._finish_command(command, CommandStatus.CANCELLED, "Robot map changed")
            return
        x, y = Decimal(str(position["x"])), Decimal(str(position["y"]))
        movement = select_movement(
            RectangleBoundary(context["dimension_x"], context["dimension_y"]),
            x, y, Decimal(command["action"]["distance"][:-1]), message.action.direction,
            width=context["dimension_x"], height=context["dimension_y"],
        )
        if movement is None:
            await self._finish_command(command, CommandStatus.FAILED, "No movement direction fits map dimensions", position=position)
            return
        direction, target_x, target_y = movement
        next_command: RobotCommandRecord = {
            **command, "attempt": message.attempt + 1,
            "action": {**command["action"], "direction": direction.value,
                       "is_start": False, "is_finish": message.attempt == 3},
            "origin": {"x": float(x), "y": float(y)},
            "target": {"x": float(target_x), "y": float(target_y)},
            "status": CommandStatus.QUEUED.value,
            "updated_at": self.clock().isoformat(), "deadline": self._command_deadline(),
        }
        if await self.robot_command_store.transition(command, next_command, position=position):
            if self.realtime_publisher is not None:
                await self.realtime_publisher.publish(position)
            try:
                await self._publish_loop_command(to_robot_command(next_command))
            except Exception:
                logger.warning(msg=f"Robot loop {message.command_id} awaits broker publication")

    async def process_ack(
        self, message: RobotAcknowledgement, *, retry_enabled: bool = True,
        on_success_ack: Callable[[RobotCommandRecord], RobotPositionRecord | None] | None = None,
    ) -> None:
        """Apply a validated ACK to the current active command."""
        command = await self._validate_active_command(message)
        if command is not None and command.get("task_id"):
            await self._process_cleaning_ack(message, command)
            return
        if command is not None:
            await apply_robot_ack(
                message, command, retry_enabled=retry_enabled, on_success_ack=on_success_ack,
                finish_command=self._finish_command, store=self.robot_command_store,
                topic=self.robot_action_topic, clock=self.clock, command_deadline=self._command_deadline,
            )

    async def process_command(self, message: RobotCommand) -> None:
        """Dispatch eligible queued or unconfirmed work through MQTT.

        Match the active command, attempt, and action first. MQTT errors try to
        restore the previous snapshot; a redelivery retains the same attempt.
        """
        active_command = await self._validate_active_command(message)
        if active_command is None or not self._command_is_due(active_command):
            return
        if active_command.get("task_id"):
            await self._dispatch_cleaning_move(message, active_command)
            return

        claim = None
        if self.cleaning_path_repository is not None:
            if active_command["status"] == CommandStatus.AWAITING_ACK.value and await self.robot_command_store.dispatched(active_command):
                return
            map_id, group_id = UUID(active_command["map_id"]), UUID(active_command["group_id"])
            layout = await self.registry.transaction_wrapper(
                lambda session: self.cleaning_path_repository.motion_layout(session, map_id, group_id),
                DbAccessContext(actor_id=None, group_id=group_id),
            )
            if layout is None:
                await self._finish_command(active_command, CommandStatus.CANCELLED, "Robot map unavailable")
                return
            blocked, guard = await self._check_movement_occupancy(active_command, layout)
            if blocked:
                return

            async def claim(pending: RobotCommandRecord) -> bool:
                if active_command["status"] == CommandStatus.AWAITING_ACK.value:
                    guard[self.robot_command_store.dispatch_key(active_command)] = None
                return await self.robot_command_store.transition(active_command, pending, guard=guard)

        await dispatch_robot_command(
            message, active_command, store=self.robot_command_store,
            gateway=self.robot_action_mqtt_gateway, clock=self.clock,
            command_deadline=self._command_deadline, claim=claim, keep_claim_on_error=claim is not None,
        )

    def _command_is_due(self, command: RobotCommandRecord) -> bool:
        """Treat unscheduled commands as due immediately."""
        dispatch_at = command.get("not_before")
        return dispatch_at is None or self.clock() >= datetime.fromisoformat(dispatch_at)

    @staticmethod
    def _movement_segment(command: RobotCommandRecord) -> LineString:
        origin, target = command["origin"], command["target"]
        return LineString([(origin["x"], origin["y"]), (target["x"], target["y"])])

    @staticmethod
    def _valid_map_point(point: object, layout: dict) -> TypeGuard[RobotCoordinates]:
        if not isinstance(point, dict):
            return False
        x, y = point.get("x"), point.get("y")
        return (
            type(x) in (int, float) and type(y) in (int, float)
            and isfinite(x) and isfinite(y)
            and 0 <= x <= layout["width"] and 0 <= y <= layout["height"]
        )

    @staticmethod
    def _shares_map(command: RobotCommandRecord, record: dict) -> bool:
        return record.get("group_id") == command["group_id"] and record.get("map_id") == command["map_id"]

    @staticmethod
    def _dock_origin(layout: dict, robot_id: str) -> RobotCoordinates | None:
        for assigned, geometry in layout.get("docks", []):
            if str(assigned) == robot_id:
                center = shape(json.loads(geometry)).centroid
                return {"x": center.x, "y": center.y}
        return None

    async def _check_movement_occupancy(
        self, command: RobotCommandRecord, layout: dict, *, confirmed_peers: set[str] | None = None,
    ) -> tuple[bool, dict[str, str | None]]:
        """Return whether the segment is blocked and the Redis values to verify at dispatch."""
        # Cleaning moves check every peer. Ordinary moves check active cleaning tasks.
        segment = self._movement_segment(command)
        clearance = settings.CLEANING_PATH_CLEARANCE_METERS
        expected_peer_values: dict[str, str | None] = {}
        for robot in layout["robots"]:
            robot_id = str(robot.id)
            if robot_id == command["robot_id"] or str(robot.group_id) != command["group_id"] or str(robot.map_id) != command["map_id"]:
                continue
            peer = await self.robot_command_store.peer_state(robot.id)
            expected_peer_values.update(peer.guard)
            peer_command = peer.command
            unconfirmed = peer.unconfirmed_movement
            # An unreadable reservation leaves the peer's occupancy unknown.
            if peer.unconfirmed_movement_invalid:
                return True, expected_peer_values
            # A saved reservation may still be occupied until execution is confirmed.
            if unconfirmed is not None:
                has_verified_origin = unconfirmed.get("origin_verified") is True
                matches_peer = unconfirmed.get("robot_id") == robot_id and self._shares_map(command, unconfirmed)
                # The reservation must have a verified origin and belong to this peer on this map.
                if not has_verified_origin or not matches_peer:
                    return True, expected_peer_values

                valid_origin = self._valid_map_point(unconfirmed.get("origin"), layout)
                valid_target = self._valid_map_point(unconfirmed.get("target"), layout)
                # Both endpoints must be valid before building the reserved segment.
                if not valid_origin or not valid_target:
                    return True, expected_peer_values

                reserved_segment = self._movement_segment(unconfirmed)
                # Block moves that enter the reserved segment's clearance zone.
                if segment.distance(reserved_segment) <= clearance:
                    return True, expected_peer_values
            if confirmed_peers is None and not peer.cleaning_task_active:
                continue
            if peer.position_invalid or peer.command_invalid:
                return True, expected_peer_values
            if peer_command is not None and peer_command.get("status") == CommandStatus.AWAITING_ACK.value:
                if (
                    not self._shares_map(command, peer_command)
                    or not self._valid_map_point(peer_command.get("origin"), layout)
                    or not self._valid_map_point(peer_command.get("target"), layout)
                    or segment.distance(self._movement_segment(peer_command)) <= clearance
                ):
                    return True, expected_peer_values
            position = peer.position
            point: RobotCoordinates | None
            # Prefer a saved position and track peers whose positions were observed during this task.
            if position is not None:
                if (
                    not self._shares_map(command, position) or position.get("robot_id") != robot_id
                ):
                    return True, expected_peer_values
                if confirmed_peers is not None:
                    confirmed_peers.add(robot_id)
                point = position
            elif confirmed_peers is None and peer_command is not None and self._shares_map(command, peer_command):
                point = peer_command.get("origin")
            # The dock is a fallback only for a peer with no observed position or outstanding movement.
            elif confirmed_peers is not None and robot_id not in confirmed_peers and unconfirmed is None:
                point = self._dock_origin(layout, robot_id)
            else:
                return True, expected_peer_values
            if not self._valid_map_point(point, layout) or segment.distance(Point(point["x"], point["y"])) <= clearance:
                return True, expected_peer_values
        return False, expected_peer_values

    async def _dispatch_cleaning_move(self, message: RobotCommand, command: RobotCommandRecord) -> None:
        """Load the task and keep the map locked through validation and publication."""
        awaiting_ack = command["status"] == CommandStatus.AWAITING_ACK.value
        if awaiting_ack and await self.robot_command_store.dispatched(command):
            return
        # Commands from older workers may lack the origin flag needed to authorize a dock fallback.
        if "origin_verified" not in command:
            await self._finish_command(command, CommandStatus.CANCELLED, "Cleaning movement has no saved origin provenance")
            return
        if settings.ROBOT_COMMANDS_PAUSED:
            if not awaiting_ack:
                await self._finish_command(command, CommandStatus.CANCELLED, "Robot commands are paused")
            return
        task = await self.robot_command_store.get_task(command["task_id"])
        if task is None or not await self.robot_command_store.has_task_moves(command["task_id"]):
            await self._finish_command(command, CommandStatus.FAILED, "Cleaning task state unavailable")
            return
        repository = self.cleaning_path_repository
        if repository is None:
            await self._finish_command(command, CommandStatus.FAILED, "Cleaning layout is unavailable")
            return
        map_id, group_id = UUID(command["map_id"]), UUID(command["group_id"])
        ctx = AppContext(
            trace_id=UUID(task.get("trace_id", command["command_id"])), action="cleaning_task",
            actor=UUID(task["actor_id"]) if task.get("actor_id") else None,
        )

        async def dispatch_with_map_lock(session: AsyncSession) -> None:
            map_locked = await repository.lock_for_dispatch(session, map_id, group_id)
            layout = await repository.layout(session, map_id, group_id, ctx) if map_locked else None
            await self._dispatch_locked_cleaning_move(message, command, task, layout)

        try:
            await self.registry.transaction_wrapper(
                dispatch_with_map_lock, DbAccessContext(actor_id=None, group_id=group_id),
            )
        except Exception:
            logger.exception(
                msg=f"Cleaning task {task['task_id']} failed to dispatch command {command['command_id']}",
                context=ctx,
            )
            raise

    async def _dispatch_locked_cleaning_move(
        self, message: RobotCommand, command: RobotCommandRecord, task: CleaningTaskRecord, layout: dict | None,
    ) -> None:
        """Validate the current layout and dispatch only if the full segment is clear."""
        task_ctx = AppContext(
            trace_id=UUID(task.get("trace_id", command["command_id"])), action="cleaning_task",
            actor=UUID(task["actor_id"]) if task.get("actor_id") else None,
        )
        if layout is None or layout_signature(layout, task_ctx) != task["layout_signature"] or not any(
            str(robot.id) == command["robot_id"] and str(robot.group_id) == command["group_id"]
            and str(robot.map_id) == command["map_id"] for robot in layout["robots"]
        ):
            if command["status"] != CommandStatus.AWAITING_ACK.value:
                await self._finish_command(command, CommandStatus.CANCELLED, "Cleaning route invalidated by map changes")
            return
        position = await self.robot_command_store.position(command["robot_id"])
        origin = None
        # Reuse a saved position only when its scope and verification match what the command expects.
        if position is not None:
            if (
                position.get("robot_id") == command["robot_id"]
                and self._shares_map(command, position)
                and (not command["origin_verified"] or position.get("verified") is True)
            ):
                origin = position
        # Only the first move can use the assigned dock when the saved position is absent.
        elif task["completed_moves"] == 0 and not command["origin_verified"]:
            origin = self._dock_origin(layout, command["robot_id"])
        # Stop if the current origin differs from the origin used to plan this move.
        if origin is None or any(origin.get(axis) != command["origin"][axis] for axis in ("x", "y")):
            await self._finish_command(command, CommandStatus.CANCELLED, "Cleaning movement no longer matches its planned origin")
            return
        confirmed_peers = set(task.get("confirmed_peer_ids", []))
        blocked, expected_peer_values = await self._check_movement_occupancy(
            command, layout, confirmed_peers=confirmed_peers,
        )
        expected_peer_values[RobotCommandStore.position_key(command["robot_id"])] = (
            RobotCommandStore.encode(position) if position is not None else None
        )
        is_occupancy_retry = task["status"] == CleaningTaskStatus.WAITING_OCCUPANCY.value
        occupancy_retries_used = task["occupancy_retries_used"] + int(is_occupancy_retry)
        if blocked:
            if command["status"] == CommandStatus.QUEUED.value:
                await self._defer_cleaning_move(
                    command, task, expected_peer_values, occupancy_retries_used, confirmed_peers,
                )
            return

        async def claim(pending: RobotCommandRecord) -> bool:
            if command["status"] == CommandStatus.AWAITING_ACK.value:
                expected_peer_values[self.robot_command_store.dispatch_key(command)] = None
            return await self._transition_cleaning_task(
                command, pending, task, status=CleaningTaskStatus.AWAITING_ACK, guard=expected_peer_values,
                retries=occupancy_retries_used, confirmed_peers=confirmed_peers,
            )

        await dispatch_robot_command(
            message, command, store=self.robot_command_store, gateway=self.robot_action_mqtt_gateway,
            clock=self.clock, command_deadline=self._command_deadline, claim=claim, keep_claim_on_error=True,
        )

    async def _defer_cleaning_move(
        self, command: RobotCommandRecord, task: CleaningTaskRecord,
        expected_peer_values: dict[str, str | None], occupancy_retries_used: int, confirmed_peers: set[str],
    ) -> None:
        """Schedule another occupancy check or fail the task when retries run out."""
        now = self.clock()
        if occupancy_retries_used >= settings.CLEANING_OCCUPANCY_RETRIES:
            reason = f"Next movement blocked after {settings.CLEANING_OCCUPANCY_RETRIES} occupancy retries"
            await self._transition_cleaning_task(
                command, {**command, "status": CommandStatus.FAILED.value, "updated_at": now.isoformat(), "failure_reason": reason},
                task, status=CleaningTaskStatus.FAILED, reason=reason, terminal=True, guard=expected_peer_values, retries=occupancy_retries_used,
            )
            return
        next_check_at = now + timedelta(seconds=settings.CLEANING_STEP_INTERVAL_SECONDS)
        deferred_command: RobotCommandRecord = {
            **command,
            "not_before": next_check_at.isoformat(),
            "updated_at": now.isoformat(),
            "deadline": (next_check_at + timedelta(seconds=settings.ROBOT_COMMAND_TIMEOUT_SECONDS)).isoformat(),
        }
        await self._transition_cleaning_task(
            command, deferred_command, task, status=CleaningTaskStatus.WAITING_OCCUPANCY,
            guard=expected_peer_values, retries=occupancy_retries_used, confirmed_peers=confirmed_peers,
        )

    def _confirmed_position(self, command: RobotCommandRecord) -> RobotPositionRecord:
        """Build confirmed coordinates for an eligible movement ACK."""
        position: RobotPositionRecord = {
            "robot_id": command["robot_id"],
            "group_id": command["group_id"],
            "map_id": command["map_id"],
            **command["target"],
            "command_id": command["command_id"],
            "updated_at": self.clock().isoformat(),
        }
        if command.get("origin_verified") is True:
            position["verified"] = True
        return position

    async def reconcile_robot(self, robot_id: UUID) -> None:
        """Expire, cancel, or recover the active command for one robot.

        Args:
            robot_id (UUID): Robot whose pending reservation should be inspected.

        Republish only eligible QUEUED work or AWAITING_ACK work without a
        confirmed MQTT dispatch marker. A confirmed send awaiting an ACK is not
        resent. Missing work is ignored; dependency errors propagate to the
        caller or the background reconciler's error handling.
        """
        record = await self.robot_command_store.active(robot_id)
        if record is None:
            return
        can_process_command = await self._can_process_command(record)
        if not can_process_command:
            return
        if not self._command_is_due(record):
            return
        if record["status"] not in (CommandStatus.QUEUED.value, CommandStatus.AWAITING_ACK.value):
            return
        if record["status"] == CommandStatus.AWAITING_ACK.value and await self.robot_command_store.dispatched(record):
            return
        command = to_robot_command(record)
        if record.get("loop"):
            await self._publish_loop_command(command)
        else:
            await self.robot_action_topic.publish_command(command)

    async def start(self) -> None:
        """Start the two shared action consumers without enumerating robots."""
        await self.robot_action_topic.start()

    async def run_reconciler(self) -> None:
        """Reconcile Redis-backed pending commands in a one-second loop.

        Log transient scan or per-robot failures and continue recovery on later
        passes. Saved deadlines remain authoritative during outages; the loop
        does not extend them or retry actions merely because their ACK is late.

        Raises:
            asyncio.CancelledError: When application shutdown cancels this task.
        """
        while True:
            try:
                async for robot_id in self.robot_command_store.active_robot_ids():
                    try:
                        await self.reconcile_robot(robot_id)
                    except Exception:
                        logger.warning(msg=f"Unable to reconcile robot command for {robot_id}")
            except Exception:
                logger.warning(msg="Robot command reconciliation is temporarily unavailable")
            await asyncio.sleep(1)
