import json
from collections.abc import Sequence
from decimal import Decimal
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.context import AppContext
from app.common.enum.map import MapStatus
from app.common.schemas.map import MapListQuery
from app.models.map import Map
from app.models.map_boundary import MapBoundary
from app.models.map_tags import map_tags
from app.models.environment_zone import EnvironmentZone
from app.models.robot import Robot
from app.models.tag import Tag


class MapRepository:
    async def get_detail_by_id_and_group(
        self,
        session: AsyncSession,
        map_id: UUID,
        group_id: UUID,
        ctx: AppContext,
    ) -> dict | None:
        """Return one active-group map and its detail resources without N+1 queries."""
        map_result = await session.execute(
            select(
                Map.id,
                Map.name,
                Map.description,
                Map.status,
                Map.dimension_x,
                Map.dimension_y,
                Map.created_at,
                Map.updated_at,
                func.ST_AsGeoJSON(MapBoundary.geometry, 17, 0).label("boundary"),
            )
            .outerjoin(MapBoundary, MapBoundary.map_id == Map.id)
            .where(Map.id == map_id, Map.group_id == group_id)
        )
        map_row = map_result.mappings().one_or_none()
        if map_row is None:
            return None

        tags_result = await session.execute(
            select(Tag.id, Tag.name)
            .select_from(map_tags)
            .join(Tag, Tag.id == map_tags.c.tag_id)
            .where(map_tags.c.map_id == map_id, Tag.group_id == group_id)
            .order_by(Tag.name.asc(), Tag.id.asc())
        )
        robots_result = await session.execute(
            select(
                Robot.id,
                Robot.name,
                Robot.serial_num,
                Robot.model,
                Robot.connection_status,
                Robot.operational_status,
            )
            .where(Robot.map_id == map_id, Robot.group_id == group_id)
            .order_by(Robot.name.asc(), Robot.id.asc())
        )
        zones_result = await session.execute(
            select(
                EnvironmentZone.id,
                EnvironmentZone.type,
                func.ST_AsGeoJSON(EnvironmentZone.geometry, 17, 0).label("geometry"),
            )
            .where(EnvironmentZone.map_id == map_id)
            .order_by(EnvironmentZone.created_at.asc(), EnvironmentZone.id.asc())
        )

        detail = dict(map_row)
        detail["boundary"] = (
            json.loads(detail["boundary"]) if detail["boundary"] is not None else None
        )
        detail["tags"] = [dict(row) for row in tags_result.mappings().all()]
        detail["robots"] = [dict(row) for row in robots_result.mappings().all()]
        detail["zones"] = []
        for row in zones_result.mappings().all():
            zone = dict(row)
            zone["geometry"] = json.loads(zone["geometry"])
            detail["zones"].append(zone)
        return detail

    async def get_by_id_and_group_for_update(
        self, session: AsyncSession, map_id: UUID, group_id: UUID, ctx: AppContext
    ) -> Map | None:
        """Lock the parent map to serialize boundary writes within its owning group."""
        result = await session.execute(
            select(Map)
            .where(Map.id == map_id, Map.group_id == group_id)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def list_maps(
        self,
        session: AsyncSession,
        query: MapListQuery,
        group_id: UUID,
        ctx: AppContext,
    ) -> tuple[list[dict], int]:
        """Return a page of active-group maps and its filtered total."""
        columns = (
            Map.id,
            Map.name,
            Map.description,
            Map.status,
            Map.created_at,
            Map.dimension_x,
            Map.dimension_y,
            Map.updated_at,
            func.ST_AsGeoJSON(MapBoundary.geometry, 17, 0).label("geometry"),
        )
        filters = [Map.group_id == group_id]
        if query.search is not None:
            filters.append(Map.name.ilike(f"%{query.search}%"))
        if query.status is not None:
            filters.append(Map.status == query.status)
        if query.tag_ids:
            filters.append(
                exists(
                    select(1)
                    .select_from(map_tags)
                    .join(Tag, Tag.id == map_tags.c.tag_id)
                    .where(
                        map_tags.c.map_id == Map.id,
                        map_tags.c.tag_id.in_(query.tag_ids),
                        Tag.group_id == group_id,
                    )
                    .correlate(Map)
                )
            )

        created_at_order = (
            Map.created_at.asc()
            if query.order_direction.value == "asc"
            else Map.created_at.desc()
        )
        stmt = (
            select(*columns)
            .outerjoin(MapBoundary, MapBoundary.map_id == Map.id)
            .where(*filters)
            .order_by(created_at_order, Map.id.asc())
            .offset((query.page - 1) * query.page_size)
            .limit(query.page_size)
        )
        count_stmt = select(func.count(func.distinct(Map.id))).where(*filters)

        result = await session.execute(stmt)
        total = (await session.execute(count_stmt)).scalar_one()
        rows = []
        for row in result.mappings().all():
            item = dict(row)
            item["geometry"] = (
                json.loads(item["geometry"]) if item["geometry"] is not None else None
            )
            rows.append(item)
        return rows, total

    async def get_by_group_and_name(
        self, session: AsyncSession, group_id: UUID, name: str, ctx: AppContext
    ) -> Map | None:
        result = await session.execute(
            select(Map).where(Map.group_id == group_id, Map.name == name)
        )
        return result.scalar_one_or_none()

    async def create_map(
        self,
        session: AsyncSession,
        *,
        group_id: UUID,
        name: str,
        description: str | None,
        dimension_x: Decimal,
        dimension_y: Decimal,
        status: MapStatus,
        tags: Sequence[Tag],
        ctx: AppContext,
    ) -> Map:
        map_record = Map(
            group_id=group_id,
            name=name,
            description=description,
            dimension_x=dimension_x,
            dimension_y=dimension_y,
            status=status,
        )
        map_record.tags = list(tags)
        session.add(map_record)
        await session.flush()
        return map_record
