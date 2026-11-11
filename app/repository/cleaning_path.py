"""Read a map layout snapshot for cleaning path previews."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.context import AppContext
from app.common.middleware.logger import Logger

from app.models.docking_station import DockingStation
from app.models.environment_zone import EnvironmentZone
from app.models.map import Map
from app.models.map_boundary import MapBoundary
from app.models.robot import Robot

logger = Logger()


class CleaningPathRepository:
    """Load map geometry and robot assignments without planning in SQL."""

    async def layout(self, session: AsyncSession, map_id: UUID, group_id: UUID,
                     ctx: AppContext) -> dict | None:
        """Return the layout fields needed for one preview request."""
        # 1. Scope the map by the active group before reading its dimensions and
        # current boundary source; absence is reported as a map-level 404.
        result = await session.execute(
            select(Map.dimension_x, Map.dimension_y, MapBoundary.source)
            .outerjoin(MapBoundary, MapBoundary.map_id == Map.id)
            .where(Map.id == map_id, Map.group_id == group_id)
        )
        row = result.one_or_none()
        if row is None:
            logger.warning(msg=f"Cleaning path map {map_id} not found in active group", context=ctx)
            return None
        # 2. Snapshot map robot IDs and connection states for authorization and
        # collision checks. The service rechecks each robot's group and map ID.
        robots = (await session.execute(
            select(Robot.id, Robot.group_id, Robot.map_id, Robot.connection_status)
            .where(Robot.map_id == map_id)
        )).all()
        # 3. Return polygons as GeoJSON for Shapely planning outside the
        # transaction. All zone types are included; the service selects blockers.
        zones = (await session.execute(
            select(EnvironmentZone.type, func.ST_AsGeoJSON(EnvironmentZone.geometry))
            .where(EnvironmentZone.map_id == map_id)
        )).all()
        # 4. Include assigned and unassigned map docks so assigned positions can
        # fall back to a centroid while every other dock remains an exclusion.
        docks = (await session.execute(
            select(DockingStation.robot_id, func.ST_AsGeoJSON(DockingStation.geometry))
            .where(DockingStation.map_id == map_id)
        )).all()
        logger.debug(msg=f"Loaded cleaning path layout: {len(robots)} robots, {len(zones)} zones, {len(docks)} docks", context=ctx)
        return {"width": row[0], "height": row[1], "boundary": row[2],
                "robots": robots, "zones": zones, "docks": docks}
