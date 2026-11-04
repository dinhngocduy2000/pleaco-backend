"""Database lookups for robot commands; transport state remains in Redis."""

from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import cast

from app.common.schemas.robot_action import RobotActionContext
from app.models.docking_station import DockingStation
from app.models.map import Map
from app.models.robot import Robot


class RobotActionRepository:
    async def list_robot_ids(self, session: AsyncSession) -> list[UUID]:
        return list(await session.scalars(select(Robot.id)))

    async def get_context(self, session: AsyncSession, robot_id: UUID) -> RobotActionContext | None:
        centroid = func.ST_Centroid(DockingStation.geometry)
        result = await session.execute(
            select(
                Robot.id.label("robot_id"), Robot.group_id, Robot.map_id,
                Robot.connection_status, Robot.operational_status,
                Map.dimension_x, Map.dimension_y,
                func.ST_X(centroid).label("dock_x"),
                func.ST_Y(centroid).label("dock_y"),
            )
            .select_from(Robot)
            .outerjoin(Map, and_(Map.id == Robot.map_id, Map.group_id == Robot.group_id))
            .outerjoin(DockingStation, and_(
                DockingStation.robot_id == Robot.id,
                DockingStation.map_id == Map.id,
            ))
            .where(Robot.id == robot_id)
        )
        row = result.mappings().one_or_none()
        return cast(RobotActionContext, dict(row)) if row is not None else None
