from collections.abc import Sequence
from decimal import Decimal
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.context import AppContext
from app.common.enum.map import MapStatus
from app.common.schemas.map import MapListQuery
from app.models.map import Map
from app.models.map_tags import map_tags
from app.models.tag import Tag


class MapRepository:
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
            .where(*filters)
            .order_by(created_at_order, Map.id.asc())
            .offset((query.page - 1) * query.page_size)
            .limit(query.page_size)
        )
        count_stmt = select(func.count(func.distinct(Map.id))).where(*filters)

        result = await session.execute(stmt)
        total = (await session.execute(count_stmt)).scalar_one()
        return [dict(row) for row in result.mappings().all()], total

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
