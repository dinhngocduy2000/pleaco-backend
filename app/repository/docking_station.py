import json
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import (
    Text,
    column,
    delete,
    exists,
    func,
    insert,
    select,
    text,
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


@dataclass(frozen=True)
class BoundaryPlacementInspection:
    """Validation results for one submitted docking-station geometry."""

    item_index: int
    boundary_exists: bool
    geometry_valid: bool
    fully_covered: bool
    edge_aligned: bool
    protrusion_allowed: bool
    snapped_geometry: dict[str, object] | None = None


class DockingStationRobotConflict(Exception):
    """The robot uniqueness constraint rejected a docking station assignment."""

    def __init__(self, index: int | None = None):
        self.index = index
        super().__init__("Robot already has a docking station")


class DockingStationRepository:
    EDGE_ALIGNMENT_TOLERANCE = 0.5

    @staticmethod
    def _geometry(geometry_json: str):
        return func.ST_SetSRID(func.ST_GeomFromGeoJSON(geometry_json), 0)

    async def inspect_boundary_placement(
        self,
        session: AsyncSession,
        map_id: UUID,
        geometry_jsons: list[str],
        ctx: AppContext,
    ) -> list[BoundaryPlacementInspection]:
        """Inspect station geometry validity, boundary alignment, and protrusion.

        Results preserve request order and contain validation flags plus the
        geometry translated to touch its closest qualifying boundary edge.
        """
        if not geometry_jsons:
            return []
        input_stations_cte = """
            input_stations AS (
                SELECT ordinality - 1 AS item_index,
                       ST_SetSRID(ST_GeomFromGeoJSON(value::text), 0) AS geometry
                FROM jsonb_array_elements(CAST(:geometries AS jsonb))
                     WITH ORDINALITY AS inputs(value, ordinality)
            )
        """
        boundary_cte = """
            boundary AS (
                SELECT geometry
                FROM map_boundaries
                WHERE map_id = :map_id
            )
        """
        checked_cte = """
            checked AS (
                SELECT input.item_index,
                       input.geometry,
                       boundary.geometry AS boundary_geometry,
                       ST_IsValid(input.geometry, 0)
                           AND NOT ST_IsEmpty(input.geometry) AS valid
                FROM input_stations AS input
                LEFT JOIN boundary ON TRUE
            )
        """
        edge_projections_cte = """
            edge_projections AS (
                -- Compare individual exterior segments; buffer distance alone would
                -- also accept a station that only approaches a workspace corner.
                SELECT checked.item_index,
                       station_edge.path AS station_edge_path,
                       workspace_edge.path AS workspace_edge_path,
                       station_edge.geom AS station_edge_geometry,
                       workspace_edge.geom AS workspace_edge_geometry,
                       ST_Distance(station_edge.geom, workspace_edge.geom) AS gap,
                       ST_ShortestLine(
                           station_edge.geom, workspace_edge.geom
                       ) AS translation_line,
                       ST_Length(ST_Intersection(
                           ST_Buffer(
                               workspace_edge.geom,
                               :tolerance,
                               'endcap=flat join=mitre'
                           ),
                           station_edge.geom
                       )) AS buffered_overlap_length,
                       ST_LineLocatePoint(
                           workspace_edge.geom,
                           ST_ClosestPoint(
                               workspace_edge.geom,
                               ST_StartPoint(station_edge.geom)
                           )
                       ) AS projected_start,
                       ST_LineLocatePoint(
                           workspace_edge.geom,
                           ST_ClosestPoint(
                               workspace_edge.geom,
                               ST_EndPoint(station_edge.geom)
                           )
                       ) AS projected_end
                FROM checked
                CROSS JOIN LATERAL ST_DumpSegments(
                    ST_ExteriorRing(checked.geometry)
                ) AS station_edge
                CROSS JOIN LATERAL ST_DumpSegments(
                    ST_Boundary(checked.boundary_geometry)
                ) AS workspace_edge
                WHERE checked.valid
                  AND checked.boundary_geometry IS NOT NULL
            )
        """
        edge_matches_cte = """
            edge_matches AS (
                -- Require both buffered contact and positive projected overlap.
                -- The latter makes a point-only corner approach fail.
                SELECT item_index,
                       station_edge_path,
                       workspace_edge_path,
                       workspace_edge_geometry,
                       gap,
                       translation_line
                FROM edge_projections
                WHERE buffered_overlap_length > 0
                  AND GREATEST(
                      0,
                      LEAST(1, GREATEST(projected_start, projected_end))
                      - GREATEST(0, LEAST(projected_start, projected_end))
                  ) > 0
            )
        """
        best_edge_match_cte = """
            best_edge_match AS (
                -- Pick the closest qualifying pair deterministically so a station
                -- near a corner is translated toward one specific boundary edge.
                SELECT DISTINCT ON (item_index)
                       item_index,
                       workspace_edge_geometry,
                       gap,
                       translation_line
                FROM edge_matches
                ORDER BY item_index, gap, station_edge_path, workspace_edge_path
            )
        """
        snapped_stations_cte = """
            snapped_stations AS (
                SELECT checked.*,
                       best_edge_match.workspace_edge_geometry AS matched_edge,
                       CASE
                           WHEN best_edge_match.translation_line IS NOT NULL
                           THEN ST_Translate(
                               checked.geometry,
                               ST_X(ST_EndPoint(best_edge_match.translation_line))
                                   - ST_X(ST_StartPoint(best_edge_match.translation_line)),
                               ST_Y(ST_EndPoint(best_edge_match.translation_line))
                                   - ST_Y(ST_StartPoint(best_edge_match.translation_line))
                           )
                           ELSE checked.geometry
                       END AS snapped_geometry
                FROM checked
                LEFT JOIN best_edge_match USING (item_index)
            )
        """
        evaluated_cte = """
            evaluated AS (
                SELECT snapped_stations.*,
                       CASE
                           WHEN boundary_geometry IS NOT NULL AND valid
                           THEN ST_Difference(snapped_geometry, boundary_geometry)
                           ELSE ST_GeomFromText('POLYGON EMPTY', 0)
                       END AS outside_geometry
                FROM snapped_stations
            )
        """
        cte_sql = ",\n".join(
            (
                input_stations_cte,
                boundary_cte,
                checked_cte,
                edge_projections_cte,
                edge_matches_cte,
                best_edge_match_cte,
                snapped_stations_cte,
                evaluated_cte,
            )
        )
        query = text(
            f"""
            -- Keep request indexes so validation errors point to the submitted item.
            WITH {cte_sql}
            SELECT item_index,
                   boundary_geometry IS NOT NULL AS boundary_exists,
                   valid,
                   COALESCE(ST_Covers(boundary_geometry, snapped_geometry), FALSE)
                       AS covered,
                   matched_edge IS NOT NULL AS edge_aligned,
                   -- The entire outside portion must fit beside one matched edge.
                   EXISTS (
                       SELECT 1
                       WHERE matched_edge IS NOT NULL
                         AND (
                             ST_IsEmpty(outside_geometry)
                             OR ST_Covers(
                                 ST_Buffer(
                                     matched_edge,
                                     :tolerance,
                                     'endcap=flat join=mitre'
                                 ),
                                 outside_geometry
                             )
                         )
                   ) AS protrusion_allowed,
                   ST_AsGeoJSON(snapped_geometry, 17, 0) AS snapped_geometry_json
            FROM evaluated
            ORDER BY item_index
            """
        )
        rows = (
            await session.execute(
                query,
                {
                    "map_id": map_id,
                    "geometries": json.dumps(
                        [json.loads(geometry_json) for geometry_json in geometry_jsons]
                    ),
                    "tolerance": self.EDGE_ALIGNMENT_TOLERANCE,
                },
            )
        ).all()
        return [
            BoundaryPlacementInspection(
                item_index=int(row.item_index),
                boundary_exists=bool(row.boundary_exists),
                geometry_valid=bool(row.valid),
                fully_covered=bool(row.covered),
                edge_aligned=bool(row.edge_aligned),
                protrusion_allowed=bool(row.protrusion_allowed),
                snapped_geometry=json.loads(row.snapped_geometry_json),
            )
            for row in rows
        ]

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

    async def get_by_id_and_map_for_update(
        self, session: AsyncSession, station_id: UUID, map_id: UUID
    ) -> DockingStation | None:
        result = await session.execute(
            select(DockingStation)
            .where(DockingStation.id == station_id, DockingStation.map_id == map_id)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def assign_robot(
        self, session: AsyncSession, station: DockingStation, robot_id: UUID
    ) -> None:
        station.robot_id = robot_id
        station.updated_at = func.now()
        try:
            await session.flush()
        except IntegrityError as error:
            cause = error.orig.__cause__
            if (
                getattr(cause, "sqlstate", None) == "23505"
                and getattr(cause, "constraint_name", None)
                == "uq_docking_station_robot_id"
            ):
                raise DockingStationRobotConflict() from error
            raise

    async def is_robot_assigned(
        self, session: AsyncSession, robot_id: UUID, station_id: UUID
    ) -> bool:
        return bool(
            await session.scalar(
                select(
                    exists().where(
                        DockingStation.robot_id == robot_id,
                        DockingStation.id != station_id,
                    )
                )
            )
        )
