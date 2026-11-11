"""Authorize and prepare a read-only cleaning path preview."""

import json
from datetime import datetime, timedelta, timezone
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
from app.common.schemas.user import Credential
from app.core.config import settings
from app.core.rbac.permissions import PermissionService
from app.core.rbac.role_validation import require_permission
from app.external.redis.robot_commands import RobotCommandStore
from app.repository.cleaning_path import CleaningPathRepository
from app.repository.registry import Registry
from app.services.cleaning_path_planner import Point2D, plan_cleaning_path

logger = Logger()
RouteSections = dict[str, list[Point2D]]


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
        logger.debug(
            msg=f"Validating cleaning path preview for map {map_id}, robot {robot_id}",
            context=ctx,
        )
        active_group_id = self._require_active_group(group_id, credential, ctx)

        # Keep the database transaction short; Redis checks and planning follow it.
        snapshot = await self._load_layout(map_id, active_group_id, ctx)
        robots = self._map_robots(snapshot, map_id, active_group_id, robot_id, ctx)
        await self._require_online(robot_id, robots[robot_id].connection_status, ctx)
        self._require_dimensions_boundary(snapshot, map_id, ctx)

        docks = self._assigned_docks(snapshot)
        own_dock = self._require_own_dock(docks, robot_id, map_id, ctx)
        positions = await self._resolve_positions(
            set(robots), docks, map_id, active_group_id, ctx
        )
        start = self._require_docked_start(positions[robot_id], own_dock, robot_id, ctx)
        exclusions = self._exclusions(snapshot, positions, robot_id)

        # Planning is the purpose of this operation; keep the call and its
        # HTTP error mapping visible in the preview flow.
        try:
            logger.debug(
                msg=f"Planning cleaning path with {len(exclusions)} exclusions",
                context=ctx,
            )
            route = plan_cleaning_path(
                float(snapshot["width"]),
                float(snapshot["height"]),
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
            return route
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
        return group_id

    async def _load_layout(self, map_id: UUID, group_id: UUID, ctx: AppContext) -> dict:
        """Load a map-scoped layout snapshot through the repository."""
        snapshot = await self.registry.transaction_wrapper(
            lambda session: self.repository.layout(session, map_id, group_id, ctx)
        )
        if snapshot is None:
            logger.warning(
                msg=f"Map {map_id} not found for cleaning path preview", context=ctx
            )
            raise NotFoundException(message="Map not found")
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
        return robots

    async def _require_online(
        self, robot_id: UUID, connection_status: RobotConnectionStatus, ctx: AppContext
    ) -> None:
        """Require both persisted ONLINE state and a fresh backend heartbeat."""
        if connection_status != RobotConnectionStatus.ONLINE:
            logger.warning(msg=f"Robot {robot_id} is not online", context=ctx)
            raise BadRequestException(
                message="Robot must be online to preview a cleaning path"
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
            raise BadRequestException(message="Robot heartbeat is missing or stale")

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

    @staticmethod
    def _assigned_docks(snapshot: dict) -> dict[UUID, BaseGeometry]:
        """Convert map-scoped assigned dock GeoJSON to Shapely polygons once."""
        return {
            robot_id: shape(json.loads(geometry))
            for robot_id, geometry in snapshot["docks"]
            if robot_id is not None
        }

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
        return dock

    async def _resolve_positions(
        self,
        robot_ids: set[UUID],
        docks: dict[UUID, BaseGeometry],
        map_id: UUID,
        group_id: UUID,
        ctx: AppContext,
    ) -> dict[UUID, Point2D]:
        """Resolve each map robot without holding a database transaction."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=60)
        positions: dict[UUID, Point2D] = {}
        for robot_id in robot_ids:
            positions[robot_id] = await self._resolve_position(
                robot_id, docks.get(robot_id), map_id, group_id, cutoff, ctx
            )
        return positions

    async def _resolve_position(
        self,
        robot_id: UUID,
        dock: BaseGeometry | None,
        map_id: UUID,
        group_id: UUID,
        cutoff: datetime,
        ctx: AppContext,
    ) -> Point2D:
        """Use a confirmed Redis point, or a dock centroid only when it is absent."""
        record = await self.positions.position(robot_id)
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
                and cutoff <= updated <= datetime.now(timezone.utc)
                and isfinite(x)
                and isfinite(y)
            )
        except (KeyError, TypeError, ValueError):
            fresh = False
        if not fresh:
            logger.warning(
                msg=f"Map robot {robot_id} confirmed position is stale", context=ctx
            )
            raise BadRequestException(
                message="A map robot has a stale confirmed position"
            )
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
        return start

    @staticmethod
    def _exclusions(
        snapshot: dict, positions: dict[UUID, Point2D], robot_id: UUID
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
        return exclusions

