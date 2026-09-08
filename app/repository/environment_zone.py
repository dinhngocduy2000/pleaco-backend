from collections.abc import Sequence
from uuid import UUID, uuid4

from sqlalchemy import case, exists, func, insert, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import CTE

from app.common.context import AppContext
from app.common.enum.environment_zone import EnvironmentZoneType
from app.models.environment_zone import EnvironmentZone
from app.models.map_boundary import MapBoundary


class EnvironmentZoneRepository:
    """Build and execute PostGIS queries for environment-zone persistence."""

    @staticmethod
    def _geometry(geometry_json: str):
        """Build a SQL expression that converts GeoJSON to local map geometry.

        ``ST_GeomFromGeoJSON`` parses the serialized polygon in PostgreSQL, and
        ``ST_SetSRID(..., 0)`` labels its coordinates as Pleco-local X/Y values.
        Setting SRID 0 does not transform or otherwise modify the coordinates.

        Args:
            geometry_json: Serialized GeoJSON Polygon from the validated request.

        Returns:
            A SQLAlchemy expression evaluated by PostGIS when its query executes.
        """
        return func.ST_SetSRID(func.ST_GeomFromGeoJSON(geometry_json), 0)

    @classmethod
    def _input_geometries(cls, geometry_jsons: Sequence[str]) -> CTE:
        """Represent a request's polygons as an indexed temporary SQL relation.

        Each input becomes one ``SELECT`` containing its zero-based request index
        and PostGIS geometry. ``UNION ALL`` combines those rows, and the named CTE
        lets later checks compare the complete batch without a query per polygon.

        Args:
            geometry_jsons: Non-empty sequence of serialized GeoJSON Polygons.

        Returns:
            The ``input_zones(zone_index, geometry)`` common table expression.

        Note:
            The request schema guarantees at least one item, which is required by
            this method and by callers that read the first validation result.
        """
        statements = [
            select(
                literal(index).label("zone_index"),
                cls._geometry(geometry_json).label("geometry"),
            )
            for index, geometry_json in enumerate(geometry_jsons)
        ]
        return union_all(*statements).cte("input_zones")

    async def inspect_boundary_coverage(
        self,
        session: AsyncSession,
        map_id: UUID,
        geometry_jsons: Sequence[str],
        ctx: AppContext,
    ) -> tuple[bool, list[tuple[int, bool, bool]]]:
        """Inspect topology and boundary coverage for every submitted polygon.

        The query returns one row per request item. ``ST_IsValid`` and
        ``ST_IsEmpty`` establish usable polygon topology. For valid polygons,
        ``ST_Covers(boundary, zone)`` verifies that every point is inside or on
        the boundary; areas outside the exterior ring or inside a boundary hole
        therefore fail. A ``CASE`` avoids evaluating coverage for invalid input,
        while ``COALESCE`` turns the missing-boundary ``NULL`` into ``False``.

        Args:
            session: Transaction-scoped asynchronous database session.
            map_id: Map whose single current boundary covers the zones.
            geometry_jsons: Non-empty GeoJSON polygons in request order.
            ctx: Request context reserved for repository tracing and logging.

        Returns:
            A pair containing whether the boundary exists and ordered triples of
            ``(zone_index, is_valid, is_covered)`` for each request item.
        """
        # Preserve request positions so the service can identify invalid items.
        inputs = self._input_geometries(geometry_jsons)

        # This Boolean remains available even when the scalar geometry below is NULL.
        boundary_exists = exists(select(1).where(MapBoundary.map_id == map_id)).label(
            "boundary_exists"
        )

        # Map boundaries are unique by map_id, so this subquery yields one geometry.
        boundary_geometry = (
            select(MapBoundary.geometry)
            .where(MapBoundary.map_id == map_id)
            .scalar_subquery()
        )
        valid = func.ST_IsValid(inputs.c.geometry, 0) & ~func.ST_IsEmpty(
            inputs.c.geometry
        )

        # ST_Covers permits contact with the boundary edge but no point outside it.
        covered = case(
            (
                valid,
                func.coalesce(
                    func.ST_Covers(boundary_geometry, inputs.c.geometry), False
                ),
            ),
            else_=False,
        )
        rows = (
            await session.execute(
                select(
                    inputs.c.zone_index,
                    boundary_exists,
                    valid.label("valid"),
                    covered.label("covered"),
                ).order_by(inputs.c.zone_index)
            )
        ).all()

        # The 1–100 item request constraint guarantees at least one result row.
        return bool(rows[0].boundary_exists), [
            (row.zone_index, bool(row.valid), bool(row.covered)) for row in rows
        ]

    async def find_batch_overlap(
        self,
        session: AsyncSession,
        geometry_jsons: Sequence[str],
        ctx: AppContext,
    ) -> tuple[int, int] | None:
        """Find the first overlapping pair inside the submitted batch.

        A self-join compares each unordered pair exactly once using
        ``left_index < right_index``. The DE-9IM mask ``T********`` requires the
        polygon interiors to intersect, so containment and identical polygons
        are conflicts while edge-only or corner-only contact remains allowed.

        Args:
            session: Transaction-scoped asynchronous database session.
            geometry_jsons: Non-empty GeoJSON polygons in request order.
            ctx: Request context reserved for repository tracing and logging.

        Returns:
            The lowest ordered pair of conflicting request indexes, or ``None``.
        """
        inputs = self._input_geometries(geometry_jsons)

        # Two aliases allow the CTE to be compared with itself as a pair matrix.
        left = inputs.alias("left_zone")
        right = inputs.alias("right_zone")
        row = (
            await session.execute(
                select(left.c.zone_index, right.c.zone_index)
                .where(
                    left.c.zone_index < right.c.zone_index,
                    func.ST_Relate(left.c.geometry, right.c.geometry, "T********"),
                )
                .order_by(left.c.zone_index, right.c.zone_index)
                .limit(1)
            )
        ).one_or_none()
        return None if row is None else (row[0], row[1])

    async def find_existing_overlap(
        self,
        session: AsyncSession,
        map_id: UUID,
        geometry_jsons: Sequence[str],
        ctx: AppContext,
    ) -> int | None:
        """Find the first submitted polygon overlapping a stored map zone.

        Only rows belonging to ``map_id`` participate. As with batch overlap,
        the DE-9IM mask checks interior intersection and allows boundary contact.
        The parent map lock acquired by the service ensures a concurrent writer
        cannot insert unchecked zones between this query and ``create_many``.

        Args:
            session: Transaction-scoped asynchronous database session.
            map_id: Map whose existing zones must remain disjoint.
            geometry_jsons: Non-empty GeoJSON polygons in request order.
            ctx: Request context reserved for repository tracing and logging.

        Returns:
            The lowest conflicting request index, or ``None`` when all are clear.
        """
        inputs = self._input_geometries(geometry_jsons)
        result = await session.execute(
            select(inputs.c.zone_index)
            .select_from(inputs)
            .join(EnvironmentZone, EnvironmentZone.map_id == map_id)
            .where(
                func.ST_Relate(inputs.c.geometry, EnvironmentZone.geometry, "T********")
            )
            .order_by(inputs.c.zone_index, EnvironmentZone.id)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def create_many(
        self,
        session: AsyncSession,
        map_id: UUID,
        zones: Sequence[tuple[EnvironmentZoneType, str]],
        ctx: AppContext,
    ) -> None:
        """Insert an already-validated zone batch with one SQL statement.

        UUIDs are generated before execution, while each GeoJSON polygon is
        converted by PostGIS in the resulting multi-row ``INSERT``. Committing
        or rolling back remains the responsibility of the surrounding service
        transaction.

        Args:
            session: Transaction-scoped asynchronous database session.
            map_id: Parent map assigned to every new zone.
            zones: Ordered ``(zone_type, geometry_json)`` values to persist.
            ctx: Request context reserved for repository tracing and logging.
        """
        await session.execute(
            insert(EnvironmentZone).values(
                [
                    {
                        "id": uuid4(),
                        "map_id": map_id,
                        "type": zone_type,
                        "geometry": self._geometry(geometry_json),
                    }
                    for zone_type, geometry_json in zones
                ]
            )
        )
