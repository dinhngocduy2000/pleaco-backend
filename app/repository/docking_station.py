import json
from uuid import UUID

from sqlalchemy import case, exists, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.context import AppContext
from app.common.enum.docking_station import DockingStationHeading
from app.models.docking_station import DockingStation
from app.models.map_boundary import MapBoundary


class DockingStationRobotConflict(Exception):
    """The robot uniqueness constraint rejected a docking station assignment."""


class DockingStationRepository:
    @staticmethod
    def _geometry(geometry_json: str):
        return func.ST_SetSRID(func.ST_GeomFromGeoJSON(geometry_json), 0)

    async def inspect_boundary_coverage(
        self, session: AsyncSession, map_id: UUID, geometry_json: str, ctx: AppContext
    ) -> tuple[bool, bool, bool]:
        geometry = self._geometry(geometry_json)
        valid = func.ST_IsValid(geometry, 0) & ~func.ST_IsEmpty(geometry)
        boundary = (
            select(MapBoundary.geometry)
            .where(MapBoundary.map_id == map_id)
            .scalar_subquery()
        )
        row = (
            await session.execute(
                select(
                    exists(select(1).where(MapBoundary.map_id == map_id)),
                    valid,
                    case(
                        (
                            valid,
                            func.coalesce(func.ST_Covers(boundary, geometry), False),
                        ),
                        else_=False,
                    ),
                )
            )
        ).one()
        return tuple(bool(value) for value in row)

    async def robot_has_station(
        self, session: AsyncSession, robot_id: UUID, ctx: AppContext
    ) -> bool:
        return bool(
            await session.scalar(
                select(exists().where(DockingStation.robot_id == robot_id))
            )
        )

    async def create(
        self,
        session: AsyncSession,
        map_id: UUID,
        robot_id: UUID | None,
        heading: DockingStationHeading,
        geometry_json: str,
        ctx: AppContext,
    ) -> dict:
        statement = (
            insert(DockingStation)
            .values(
                map_id=map_id,
                robot_id=robot_id,
                heading=heading,
                geometry=self._geometry(geometry_json),
            )
            .returning(
                DockingStation.id,
                DockingStation.map_id,
                DockingStation.robot_id,
                DockingStation.heading,
                func.ST_AsGeoJSON(DockingStation.geometry, 17, 0).label("geometry"),
                DockingStation.created_at,
                DockingStation.updated_at,
            )
        )
        # A savepoint leaves the caller's transaction usable after a constraint failure.
        try:
            async with session.begin_nested():
                row = dict((await session.execute(statement)).mappings().one())
        except IntegrityError as error:
            # asyncpg exposes constraint metadata on the adapted exception's cause.
            cause = error.orig.__cause__
            if (
                getattr(cause, "sqlstate", None) == "23505"
                and getattr(cause, "constraint_name", None)
                == "uq_docking_station_robot_id"
            ):
                raise DockingStationRobotConflict() from error
            raise
        row["geometry"] = json.loads(row["geometry"])
        return row
