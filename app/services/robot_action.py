"""Robot action lifecycle, authorization, and confirmed-position reconciliation."""

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from app.common.context import AppContext
from app.common.enum.robot import RobotConnectionStatus
from app.common.enum.user_roles import GroupRole
from app.common.exceptions import BadRequestException, ForbiddenException, NotFoundException
from app.common.middleware.logger import Logger
from app.common.schemas.robot_action import (
    CommandStatus, RobotAcknowledgement, RobotCommand, RobotCommandInfo, RobotPosition,
    RobotActionContext, RobotCommandRecord, RobotCommandRequest, RobotPositionRecord,
    RobotMovementCoordinates,
)
from app.common.schemas.user import Credential
from app.core.config import settings
from app.core.rbac.permissions import PermissionService
from app.core.rbac.role_validation import require_permission
from app.external.mqtt.robot_action import RobotActionMqttGateway
from app.external.queues.topics.robot_action import RobotActionTopic
from app.external.redis.robot_commands import RobotCommandStore
from app.repository.registry import Registry

logger = Logger()


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
        self.robot_action_topic.set_handler(self.process_message)

    def _command_deadline(self) -> str:
        """Return a new ISO 8601 deadline using the configured command timeout."""
        return (self.clock() + timedelta(seconds=settings.ROBOT_COMMAND_TIMEOUT_SECONDS)).isoformat()

    async def _get_robot_context(self, robot_id: UUID) -> RobotActionContext | None:
        """Load persisted robot, map, and docking facts in a short transaction.

        Args:
            robot_id (UUID): Canonical robot identifier.

        Returns:
            dict | None: Ownership, connectivity, map dimensions, and docking
            centroid, or None when the robot does not exist.
        """
        return await self.registry.transaction_wrapper(
            lambda session: self.registry.robot_action_repo().get_context(session, robot_id)
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
        context = await self._get_robot_context(robot_id)
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
        if context["map_id"] is None or context["dimension_x"] is None:
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
        context = await self._get_group_robot_context(robot_id, credential, group_id)
        await self._ensure_robot_ready(robot_id=robot_id, context=context)
        position = await self.robot_command_store.position(robot_id)
        origin_x, origin_y = self._resolve_origin(
            position=position,
            context=context,
            group_id=group_id,
        )
        distance = Decimal(request.action.distance[:-1])
        step_x, step_y = {"NORTH": (0, 1), "SOUTH": (0, -1), "EAST": (1, 0), "WEST": (-1, 0)}[request.action.direction]
        target_x, target_y = origin_x + step_x * distance, origin_y + step_y * distance
        self._validate_movement(
            context=context,
            coordinates=RobotMovementCoordinates(
                origin_x=origin_x,
                origin_y=origin_y,
                target_x=target_x,
                target_y=target_y,
            ),
        )
        # Ensure routing exists before reserving/publishing command intent.
        await self.robot_action_topic.ensure_robot(robot_id)
        created_at = self.clock().isoformat()
        command = RobotCommand(robot_id=robot_id, command_id=uuid4(), attempt=1, action=request.action)
        record: RobotCommandRecord = {
            **command.model_dump(mode="json"), "group_id": str(group_id),
            "map_id": str(context["map_id"]), "origin": {"x": float(origin_x), "y": float(origin_y)},
            "target": {"x": float(target_x), "y": float(target_y)},
            "status": CommandStatus.QUEUED.value, "created_at": created_at, "updated_at": created_at,
            "deadline": self._command_deadline(), "failure_reason": None,
        }
        if not await self.robot_command_store.reserve(record, position):
            raise BadRequestException(message="Robot is busy or its position changed; retry the request", status_code=409)
        try:
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
            An omitted position leaves the existing position and its TTL intact.
        """
        updated_record: RobotCommandRecord = {**record, "status": status.value, "updated_at": self.clock().isoformat(), "failure_reason": reason}
        return await self.robot_command_store.transition(record, updated_record, terminal=True, position=position)

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

    async def process_message(self, message: RobotCommand) -> None:
        """Process one validated command or execution ACK from a robot queue.

        Match the saved command, attempt, and action before checking eligibility.
        Ignore unmatched or terminal work. Successful ACKs atomically save the
        target position; negative ACKs queue the next attempt or fail attempt 4.
        Dispatch messages send eligible queued or unconfirmed work through MQTT
        under the saved deadline, recording confirmed transport publication.

        Args:
            message: RobotCommand to dispatch, or its RobotAcknowledgement
                subclass containing the execution outcome.

        Transport and persistence errors propagate to the queue consumer's retry
        policy. MQTT dispatch errors attempt to restore the previous snapshot;
        retries keep the same command/attempt identity for device deduplication.
        A transport confirmation alone does not update the robot's position.
        """
        active_command = await self.robot_command_store.active(message.robot_id)
        if active_command is None:
            return
        saved_command = {field: active_command[field] for field in RobotCommand.model_fields}
        received_command = message.model_dump(mode="json", exclude={"ack"})
        if saved_command != received_command or not await self._can_process_command(active_command):
            return
        if isinstance(message, RobotAcknowledgement):
            if active_command["status"] != CommandStatus.AWAITING_ACK.value:
                return
            if message.ack:
                confirmed_position: RobotPositionRecord = {
                    "robot_id": active_command["robot_id"],
                    "group_id": active_command["group_id"],
                    "map_id": active_command["map_id"],
                    **active_command["target"],
                    "command_id": active_command["command_id"],
                    "updated_at": self.clock().isoformat(),
                }
                await self._finish_command(active_command, CommandStatus.SUCCEEDED, position=confirmed_position)
            elif message.attempt >= 4:
                await self._finish_command(active_command, CommandStatus.FAILED, "Robot action failed after three retries")
            else:
                retry_command: RobotCommandRecord = {
                    **active_command,
                    "attempt": message.attempt + 1,
                    "status": CommandStatus.QUEUED.value,
                    "updated_at": self.clock().isoformat(),
                    "deadline": self._command_deadline(),
                }
                if await self.robot_command_store.transition(active_command, retry_command):
                    # On publication failure reconciliation resumes this saved intent.
                    await self.robot_action_topic.publish_command(self._to_robot_command(retry_command))
            return
        if active_command["status"] == CommandStatus.QUEUED.value:
            pending_command: RobotCommandRecord = {
                **active_command,
                "status": CommandStatus.AWAITING_ACK.value,
                "updated_at": self.clock().isoformat(),
                "deadline": self._command_deadline(),
            }
            if not await self.robot_command_store.transition(active_command, pending_command):
                return
        elif (
            active_command["status"] == CommandStatus.AWAITING_ACK.value
            and not await self.robot_command_store.dispatched(active_command)
        ):
            # Recover a crash between saving dispatch intent and confirming
            # MQTT publication; preserve the original acknowledgement deadline.
            pending_command = active_command
        else:
            return
        try:
            await self.robot_action_mqtt_gateway.publish_command(
                message, deadline=datetime.fromisoformat(pending_command["deadline"])
            )
            await self.robot_command_store.mark_dispatched(pending_command)
        except Exception:
            # An uncertain MQTT publish may have delivered: same attempt is
            # deduplicated by the robot, while the original deadline stays bounded.
            await self.robot_command_store.transition(pending_command, active_command)
            raise

    @staticmethod
    def _to_robot_command(record: RobotCommandRecord) -> RobotCommand:
        """Extract the device-facing command fields from an internal record.

        Args:
            record: Saved command including internal state and navigation facts.

        Returns:
            RobotCommand: Validated MQTT/RabbitMQ command envelope without
            backend-only lifecycle, ownership, or coordinate metadata.

        Raises:
            KeyError: If a required command field is missing from the record.
            ValidationError: If the extracted command fields are invalid.
        """
        return RobotCommand.model_validate({key: record[key] for key in RobotCommand.model_fields})

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
        if record is not None and await self._can_process_command(record):
            if record["status"] == CommandStatus.QUEUED.value or (
                record["status"] == CommandStatus.AWAITING_ACK.value and not await self.robot_command_store.dispatched(record)
            ):
                await self.robot_action_topic.publish_command(self._to_robot_command(record))

    async def start(self) -> None:
        """Restore action-queue subscriptions for all registered robot UUIDs.

        Successful subscriptions are cached by the topic adapter, so retrying a
        partially failed startup preserves existing consumers. Repository and
        subscription errors propagate to the application's background startup
        retry loop. This method does not dispatch commands or move positions.
        """
        robot_ids = await self.registry.transaction_wrapper(lambda session: self.registry.robot_action_repo().list_robot_ids(session))
        for robot_id in robot_ids:
            await self.robot_action_topic.ensure_robot(robot_id)

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
