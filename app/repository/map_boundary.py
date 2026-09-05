import json
from uuid import UUID, uuid4

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.common.context import AppContext
from app.common.enum.map import MapBoundarySource
from app.models.map_boundary import MapBoundary


class MapBoundaryRepository:
    @staticmethod
    def _geometry(geometry_json: str) -> ColumnElement:
        # Geometry-shaped JSON represents local coordinates, not WGS84.
        return func.ST_SetSRID(func.ST_GeomFromGeoJSON(geometry_json), 0)

    async def inspect_geometry(
        self,
        session: AsyncSession,
        geometry_json: str,
        dimension_x: float,
        dimension_y: float,
        ctx: AppContext,
    ) -> tuple[bool, bool]:
        """Check topology before evaluating coverage of a potentially invalid polygon."""
        geometry = self._geometry(geometry_json)
        valid = func.ST_IsValid(geometry, 0) & ~func.ST_IsEmpty(geometry)
        envelope = func.ST_MakeEnvelope(0, 0, dimension_x, dimension_y, 0)
        result = await session.execute(
            select(
                valid,
                case((valid, func.ST_Covers(envelope, geometry)), else_=False),
            )
        )
        is_valid, is_covered = result.one()
        return bool(is_valid), bool(is_covered)

    async def upsert(
        self,
        session: AsyncSession,
        map_id: UUID,
        source: MapBoundarySource,
        geometry_json: str,
        ctx: AppContext,
    ) -> dict:
        """Save under the caller's parent-map lock, preserving existing identity."""
        statement = insert(MapBoundary).values(
            id=uuid4(), map_id=map_id, source=source,
            geometry=self._geometry(geometry_json),
        )
        statement = statement.on_conflict_do_update(
            constraint="uq_map_boundaries_map_id",
            set_={
                "source": statement.excluded.source,
                "geometry": statement.excluded.geometry,
                "updated_at": func.now(),
            },
        ).returning(
            MapBoundary.id,
            MapBoundary.map_id,
            MapBoundary.source,
            func.ST_AsGeoJSON(MapBoundary.geometry, 17, 0).label("geometry"),
            MapBoundary.created_at,
            MapBoundary.updated_at,
        )
        row = dict((await session.execute(statement)).mappings().one())
        row["geometry"] = json.loads(row["geometry"])
        return row
