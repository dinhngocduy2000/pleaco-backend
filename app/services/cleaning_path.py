"""Plan cleaning routes for preview and execution."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from math import isfinite
from typing import Protocol
from uuid import UUID

from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry

from app.common.context import AppContext
from app.common.enum.environment_zone import EnvironmentZoneType
from app.common.enum.map import MapBoundarySource
from app.common.enum.robot import RobotConnectionStatus
from app.common.enum.user_roles import GroupRole
from app.common.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
)
from app.common.middleware.logger import Logger
from app.common.schemas.robot_action import RobotPositionRecord
from app.common.schemas.user import Credential
from app.core.config import settings
from app.core.rbac.permissions import PermissionService
from app.core.rbac.role_validation import require_permission
from app.external.redis.robot_commands import RobotCommandStore
from app.repository.cleaning_path import CleaningPathRepository
from app.repository.registry import DbAccessContext, Registry
from app.services.cleaning_path_planner import Point2D, plan_cleaning_path

logger = Logger()
RouteSections = dict[str, list[Point2D]]


@dataclass(frozen=True)
class PlannedCleaningPath:
    """Route geometry and the planning inputs needed to reserve and revalidate it."""

    sections: RouteSections
    start: Point2D
    center: Point2D
    start_position: RobotPositionRecord | None
    layout_signature: str
    confirmed_peer_ids: list[str]


def layout_signature(snapshot: dict, ctx: AppContext | None = None) -> str:
    """Identify the geometry a saved route depends on, independent of row order."""
    geometry = {
        "width": str(snapshot["width"]),
        "height": str(snapshot["height"]),
        "boundary": str(snapshot["boundary"]),
        "clearance": settings.CLEANING_PATH_CLEARANCE_METERS,
        "obstacles": sorted(
            (str(zone_type), geometry)
            for zone_type, geometry in snapshot["zones"]
            if zone_type in (EnvironmentZoneType.NO_GO, EnvironmentZoneType.OBSTACLE)
        ),
        "docks": sorted((str(robot_id or ""), geometry) for robot_id, geometry in snapshot["docks"]),
    }
    signature = sha256(json.dumps(geometry, sort_keys=True).encode()).hexdigest()
    logger.debug(msg="Computed cleaning layout signature", context=ctx)
    return signature


class MapRobot(Protocol):
    """Robot fields required from the repository's layout snapshot."""

    id: UUID
    group_id: UUID
    map_id: UUID | None
    connection_status: RobotConnectionStatus


class CleaningPathService:
    """Coordinate authorization, layout and position checks, and planning."""

    def __init__(
        self,
        registry: Registry,
        permission_service: PermissionService,
        positions: RobotCommandStore,
        repository: CleaningPathRepository,
    ) -> None:
        self.registry = registry
        self.permission_service = permission_service
        self.positions = positions
        self.repository = repository

    @require_permission(GroupRole.MEMBER)
    async def preview(
        self,
        *,
        map_id: UUID,
        robot_id: UUID,
        group_id: UUID | None,
        credential: Credential,
        ctx: AppContext,
    ) -> RouteSections:
        """Return ordered ring sections without changing robot or task state."""
        logger.debug(msg=f"Preparing cleaning path preview for map {map_id}, robot {robot_id}", context=ctx)
        active_group_id = self._require_active_group(group_id, credential, ctx)
        planned = await self.plan_for_robot(
            map_id=map_id, robot_id=robot_id, group_id=active_group_id,
            actor_id=credential.id, ctx=ctx,
        )
        logger.debug(msg=f"Prepared {len(planned.sections)} preview sections", context=ctx)
        return planned.sections

    async def plan_for_robot(
        self, *, map_id: UUID, robot_id: UUID, group_id: UUID, actor_id: UUID,
        ctx: AppContext, offline_status_code: int = 400, for_execution: bool = False,
    ) -> PlannedCleaningPath:
        """Generate a scoped route and capture the position used as its start."""
        logger.info(msg=f"Preparing cleaning path for map {map_id}, robot {robot_id}", context=ctx)
        # Keep the database transaction short; Redis checks and planning follow it.
        snapshot = await self._load_layout(map_id, group_id, actor_id, ctx)
        robots = self._map_robots(snapshot, map_id, group_id, robot_id, ctx)
        await self._require_online(
            robot_id, robots[robot_id].connection_status, ctx, offline_status_code
        )
        self._require_dimensions_boundary(snapshot, map_id, ctx)

        docks = self._assigned_docks(snapshot, ctx)
        own_dock = self._require_own_dock(docks, robot_id, map_id, ctx)
        position_records: dict[UUID, RobotPositionRecord | None] = {}
        positions: dict[UUID, Point2D] = {}
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=60)
        for map_robot_id in robots:
            position_record = await self.positions.position(map_robot_id)
            position_records[map_robot_id] = position_record
            positions[map_robot_id] = self._resolve_position(
                map_robot_id, position_record, docks.get(map_robot_id), map_id, group_id, cutoff, ctx,
                for_execution=for_execution,
            )
        start = self._require_docked_start(positions[robot_id], own_dock, robot_id, ctx)
        exclusions = self._exclusions(snapshot, positions, robot_id, ctx)
        width, height = float(snapshot["width"]), float(snapshot["height"])

        # Planning is the purpose of this operation; keep the call and its
        # HTTP error mapping visible in the preview flow.
        try:
            logger.debug(
                msg=f"Planning cleaning path with {len(exclusions)} exclusions",
                context=ctx,
            )
            route = plan_cleaning_path(
                width,
                height,
                start,
                exclusions,
                clearance=settings.CLEANING_PATH_CLEARANCE_METERS,
                own_dock=own_dock,
                ctx=ctx,
            )
            logger.info(
                msg=f"Cleaning path planned with {len(route)} route sections",
                context=ctx,
            )
            return PlannedCleaningPath(
                sections=route, start=start, center=(width / 2, height / 2),
                # Reservation compares the original Redis record, including its timestamp.
                start_position=position_records[robot_id], layout_signature=layout_signature(snapshot, ctx),
                confirmed_peer_ids=sorted(
                    str(peer_id) for peer_id, record in position_records.items()
                    if peer_id != robot_id and record is not None
                ),
            )
        except ValueError as exc:
            logger.warning(msg=f"Cleaning path planning failed: {exc}", context=ctx)
            raise BadRequestException(message=str(exc)) from exc

    @staticmethod
    def _require_active_group(
        group_id: UUID | None, credential: Credential, ctx: AppContext
    ) -> UUID:
        """Bind the request to the authenticated caller's selected group."""
        if group_id is None or group_id != credential.active_group_id:
            logger.warning(
                msg="Cleaning path preview has no matching active group", context=ctx
            )
            raise ForbiddenException(message="An active group must be selected")
        logger.debug(msg="Validated active group for cleaning path", context=ctx)
        return group_id

    async def _load_layout(
        self, map_id: UUID, group_id: UUID, actor_id: UUID, ctx: AppContext
    ) -> dict:
        """Load a map-scoped layout snapshot through the repository."""
        logger.debug(msg=f"Loading cleaning layout for map {map_id}", context=ctx)
        snapshot = await self.registry.transaction_wrapper(
            lambda session: self.repository.layout(session, map_id, group_id, ctx),
            DbAccessContext(actor_id=actor_id, group_id=group_id),
        )
        if snapshot is None:
            logger.warning(
                msg=f"Map {map_id} not found for cleaning path preview", context=ctx
            )
            raise NotFoundException(message="Map not found")
        logger.debug(msg=f"Loaded cleaning layout for map {map_id}", context=ctx)
        return snapshot

    @staticmethod
    def _map_robots(
        snapshot: dict, map_id: UUID, group_id: UUID, robot_id: UUID, ctx: AppContext
    ) -> dict[UUID, MapRobot]:
        """Limit robot checks to the caller's map and group."""
        robots = {
            row.id: row
            for row in snapshot["robots"]
            if row.group_id == group_id and row.map_id == map_id
        }
        if robot_id not in robots:
            logger.warning(
                msg=f"Robot {robot_id} is not included in map {map_id}", context=ctx
            )
            raise NotFoundException(message="Robot is not included in this map")
        logger.debug(msg=f"Validated {len(robots)} robots on map {map_id}", context=ctx)
        return robots

    async def _require_online(
        self, robot_id: UUID, connection_status: RobotConnectionStatus, ctx: AppContext,
        offline_status_code: int,
    ) -> None:
        """Require both persisted ONLINE state and a fresh backend heartbeat."""
        if connection_status != RobotConnectionStatus.ONLINE:
            logger.warning(msg=f"Robot {robot_id} is not online", context=ctx)
            raise BadRequestException(
                message="Robot must be online to plan a cleaning path",
                status_code=offline_status_code,
            )
        heartbeat = await self.positions.redis_client.get(
            f"robot:{robot_id}:last_seen_at"
        )
        try:
            last_seen = datetime.fromisoformat(heartbeat) if heartbeat else None
            fresh = (
                last_seen is not None
                and last_seen.tzinfo is not None
                and 0
                <= (datetime.now(timezone.utc) - last_seen).total_seconds()
                <= settings.HEARTBEAT_REDIS_TTL_SECONDS
            )
        except (TypeError, ValueError):
            fresh = False
        if not fresh:
            logger.warning(
                msg=f"Robot {robot_id} heartbeat missing or stale", context=ctx
            )
            raise BadRequestException(
                message="Robot heartbeat is missing or stale", status_code=offline_status_code
            )
        logger.debug(msg=f"Robot {robot_id} has a fresh heartbeat", context=ctx)

    @staticmethod
    def _require_dimensions_boundary(
        snapshot: dict, map_id: UUID, ctx: AppContext
    ) -> None:
        """Reject boundary sources the rectangular planner cannot handle."""
        if snapshot["boundary"] != MapBoundarySource.DIMENSIONS:
            logger.warning(
                msg=f"Map {map_id} has unsupported cleaning path boundary", context=ctx
            )
            raise BadRequestException(
                message="Only DIMENSIONS map boundaries are supported"
            )
        logger.debug(msg=f"Validated rectangular boundary for map {map_id}", context=ctx)

    @staticmethod
    def _assigned_docks(snapshot: dict, ctx: AppContext | None = None) -> dict[UUID, BaseGeometry]:
        """Convert map-scoped assigned dock GeoJSON to Shapely polygons once."""
        docks = {
            robot_id: shape(json.loads(geometry))
            for robot_id, geometry in snapshot["docks"]
            if robot_id is not None
        }
        logger.debug(msg=f"Resolved {len(docks)} assigned docks", context=ctx)
        return docks

    @staticmethod
    def _require_own_dock(
        docks: dict[UUID, BaseGeometry], robot_id: UUID, map_id: UUID, ctx: AppContext
    ) -> BaseGeometry:
        """Require an assigned dock before resolving the robot's start point."""
        dock = docks.get(robot_id)
        if dock is None:
            logger.warning(
                msg=f"Robot {robot_id} has no assigned dock on map {map_id}",
                context=ctx,
            )
            raise BadRequestException(
                message="Robot requires an assigned docking station on this map"
            )
        logger.debug(msg=f"Validated assigned dock for robot {robot_id}", context=ctx)
        return dock

    @staticmethod
    def _resolve_position(
        robot_id: UUID,
        record: RobotPositionRecord | None,
        dock: BaseGeometry | None,
        map_id: UUID,
        group_id: UUID,
        cutoff: datetime,
        ctx: AppContext,
        *,
        for_execution: bool,
    ) -> Point2D:
        """Use verified evidence for execution and allow an absent preview point at its dock."""
        if for_execution and (
            record is None or record.get("verified") is not True or record.get("robot_id") != str(robot_id)
        ):
            logger.warning(msg=f"Map robot {robot_id} has no verified position for execution", context=ctx)
            raise BadRequestException(message="A map robot has no verified position", status_code=409)
        if record is None:
            if dock is None:
                logger.warning(
                    msg=f"Map robot {robot_id} has no confirmed position or assigned dock",
                    context=ctx,
                )
                raise BadRequestException(
                    message="A map robot has no confirmed position or assigned docking station"
                )
            center = dock.centroid
            logger.info(
                msg=f"Map robot {robot_id} has no confirmed position; using assigned dock centroid",
                context=ctx,
            )
            return float(center.x), float(center.y)

        # A record from another group or map is invalid, even if its timestamp is fresh.
        if record.get("group_id") != str(group_id) or record.get("map_id") != str(
            map_id
        ):
            logger.warning(
                msg=f"Map robot {robot_id} has no matching confirmed position",
                context=ctx,
            )
            raise BadRequestException(message="A map robot has no confirmed position")
        try:
            updated = datetime.fromisoformat(
                record["updated_at"].replace("Z", "+00:00")
            )
            x, y = float(record["x"]), float(record["y"])
            fresh = (
                updated.tzinfo is not None
                and updated <= datetime.now(timezone.utc)
                and (record.get("verified") is True or cutoff <= updated)
                and isfinite(x)
                and isfinite(y)
            )
        except (KeyError, TypeError, ValueError):
            fresh = False
        if not fresh:
            logger.warning(
                msg=f"Map robot {robot_id} confirmed position is invalid or stale", context=ctx
            )
            raise BadRequestException(
                message="A map robot has a stale confirmed position"
            )
        logger.debug(msg=f"Resolved confirmed position for robot {robot_id}", context=ctx)
        return x, y

    @staticmethod
    def _require_docked_start(
        start: Point2D, dock: BaseGeometry, robot_id: UUID, ctx: AppContext
    ) -> Point2D:
        """Ensure the first ring point is covered by the assigned dock."""
        if not dock.covers(Point(start)):
            logger.warning(
                msg=f"Robot {robot_id} confirmed position is outside assigned dock",
                context=ctx,
            )
            raise BadRequestException(
                message="Confirmed robot position must be inside its dock"
            )
        logger.debug(msg=f"Validated docked start for robot {robot_id}", context=ctx)
        return start

    @staticmethod
    def _exclusions(
        snapshot: dict, positions: dict[UUID, Point2D], robot_id: UUID, ctx: AppContext | None = None
    ) -> list[BaseGeometry]:
        """Keep cleaning zones traversable; block hazards, docks, and other robots."""
        exclusions = [
            shape(json.loads(geometry))
            for zone_type, geometry in snapshot["zones"]
            if zone_type in (EnvironmentZoneType.NO_GO, EnvironmentZoneType.OBSTACLE)
        ]
        exclusions.extend(
            shape(json.loads(geometry))
            for assigned, geometry in snapshot["docks"]
            if assigned != robot_id
        )
        exclusions.extend(
            Point(position)
            for current_id, position in positions.items()
            if current_id != robot_id
        )
        logger.debug(msg=f"Built {len(exclusions)} cleaning path exclusions", context=ctx)
        return exclusions

