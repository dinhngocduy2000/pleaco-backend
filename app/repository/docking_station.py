import json
from uuid import UUID, uuid4

from sqlalchemy import (
    Text,
    case,
    column,
    delete,
    exists,
    func,
    insert,
    literal,
    select,
    union_all,
    update,
    values,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQL_UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.context import AppContext
from app.common.enum.docking_station import DockingStationHeading
from app.models.docking_station import DockingStation
from app.models.map_boundary import MapBoundary


class DockingStationRobotConflict(Exception):
    """The robot uniqueness constraint rejected a docking station assignment."""

    def __init__(self, index: int | None = None):
        self.index = index
        super().__init__("Robot already has a docking station")


class DockingStationRepository:
    @staticmethod
    def _geometry(geometry_json: str):
        return func.ST_SetSRID(func.ST_GeomFromGeoJSON(geometry_json), 0)

    async def inspect_boundary_coverage(
        self,
        session: AsyncSession,
        map_id: UUID,
        geometry_jsons: list[str],
        ctx: AppContext,
    ) -> list[tuple[bool, bool, bool]]:
        if not geometry_jsons:
            return []
        inputs = union_all(
            *[
                select(
                    literal(index).label("item_index"),
                    self._geometry(geometry_json).label("geometry"),
                )
                for index, geometry_json in enumerate(geometry_jsons)
            ]
        ).cte("input_stations")
        geometry = inputs.c.geometry
        valid = func.ST_IsValid(geometry, 0) & ~func.ST_IsEmpty(geometry)
        boundary = (
            select(MapBoundary.geometry)
            .where(MapBoundary.map_id == map_id)
            .scalar_subquery()
        )
        rows = (
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
                ).order_by(inputs.c.item_index)
            )
        ).all()
        return [(bool(row[0]), bool(row[1]), bool(row[2])) for row in rows]

    async def robots_with_other_map_stations(
        self,
        session: AsyncSession,
        robot_ids: list[UUID],
        map_id: UUID,
        ctx: AppContext,
    ) -> set[UUID]:
        if not robot_ids:
            return set()
        return set(
            await session.scalars(
                select(DockingStation.robot_id).where(
                    DockingStation.robot_id.in_(robot_ids),
                    DockingStation.map_id != map_id,
                )
            )
        )

    async def list_for_map(
        self, session: AsyncSession, map_id: UUID, ctx: AppContext
    ) -> dict[UUID, UUID | None]:
        rows = await session.execute(
            select(DockingStation.id, DockingStation.robot_id).where(
                DockingStation.map_id == map_id
            )
        )
        return dict(rows.all())

    async def delete_many(
        self,
        session: AsyncSession,
        map_id: UUID,
        station_ids: list[UUID],
        ctx: AppContext,
    ) -> None:
        if station_ids:
            await session.execute(
                delete(DockingStation).where(
                    DockingStation.map_id == map_id, DockingStation.id.in_(station_ids)
                )
            )

    async def has_outside_boundary(
        self,
        session: AsyncSession,
        map_id: UUID,
        ctx: AppContext,
    ) -> bool:
        """Return whether any stored station is outside the current boundary."""
        boundary = (
            select(MapBoundary.geometry)
            .where(MapBoundary.map_id == map_id)
            .scalar_subquery()
        )
        statement = select(
            exists(
                select(1).where(
                    DockingStation.map_id == map_id,
                    func.coalesce(
                        func.ST_Covers(boundary, DockingStation.geometry), False
                    ).is_(False),
                )
            )
        )
        return bool(await session.scalar(statement))

    async def clear_assignments(
        self,
        session: AsyncSession,
        map_id: UUID,
        station_ids: list[UUID],
        ctx: AppContext,
    ) -> None:
        if station_ids:
            await session.execute(
                update(DockingStation)
                .where(
                    DockingStation.map_id == map_id, DockingStation.id.in_(station_ids)
                )
                .values(robot_id=None)
            )

    async def save_many(
        self,
        session: AsyncSession,
        map_id: UUID,
        stations: list[tuple[UUID | None, UUID | None, DockingStationHeading, str]],
        ctx: AppContext,
    ) -> list[dict]:
        """Write the validated snapshot with one UPDATE and one INSERT at most."""
        if not stations:
            return []
        ids = [station_id or uuid4() for station_id, _, _, _ in stations]
        new_rows = []
        edits = []
        for station_id, (existing_id, robot_id, heading, geometry_json) in zip(
            ids, stations
        ):
            if existing_id is None:
                new_rows.append(
                    dict(
                        id=station_id,
                        map_id=map_id,
                        robot_id=robot_id,
                        heading=heading,
                        geometry=self._geometry(geometry_json),
                    )
                )
            else:
                edits.append((station_id, robot_id, heading, geometry_json))
        table = DockingStation.__table__
        returning = [
            table.c.id,
            table.c.map_id,
            table.c.robot_id,
            table.c.heading,
            func.ST_AsGeoJSON(table.c.geometry, 17, 0).label("geometry"),
            table.c.created_at,
            table.c.updated_at,
        ]
        rows = []
        try:
            async with session.begin_nested():
                if edits:
                    inputs = values(
                        column("id", PostgreSQL_UUID(as_uuid=True)),
                        column("robot_id", PostgreSQL_UUID(as_uuid=True)),
                        column("heading", table.c.heading.type),
                        column("geometry_json", Text),
                        name="station_updates",
                    ).data(edits)
                    statement = (
                        update(table)
                        .where(
                            table.c.map_id == map_id,
                            table.c.id == inputs.c.id,
                        )
                        .values(
                            robot_id=inputs.c.robot_id.cast(table.c.robot_id.type),
                            heading=inputs.c.heading.cast(table.c.heading.type),
                            geometry=self._geometry(inputs.c.geometry_json),
                            updated_at=func.now(),
                        )
                        .returning(*returning)
                    )
                    rows.extend((await session.execute(statement)).mappings().all())
                if new_rows:
                    rows.extend(
                        (
                            await session.execute(
                                insert(table).values(new_rows).returning(*returning)
                            )
                        )
                        .mappings()
                        .all()
                    )
        except IntegrityError as error:
            cause = error.orig.__cause__
            if (
                getattr(cause, "sqlstate", None) != "23505"
                or getattr(cause, "constraint_name", None)
                != "uq_docking_station_robot_id"
            ):
                raise
            # The savepoint rolled back every write. Resolve the conflicting item in
            # one query without parsing PostgreSQL's potentially sensitive error detail.
            robot_ids = [
                robot_id for _, robot_id, _, _ in stations if robot_id is not None
            ]
            assigned = dict(
                (
                    await session.execute(
                        select(table.c.robot_id, table.c.id).where(
                            table.c.robot_id.in_(robot_ids)
                        )
                    )
                ).all()
            )
            index = next(
                (
                    index
                    for index, (station_id, (_, robot_id, _, _)) in enumerate(
                        zip(ids, stations)
                    )
                    if robot_id in assigned and assigned[robot_id] != station_id
                ),
                None,
            )
            raise DockingStationRobotConflict(index=index) from error
        by_id = {row["id"]: dict(row) for row in rows}
        for row in by_id.values():
            row["geometry"] = json.loads(row["geometry"])
        return [by_id[station_id] for station_id in ids]
