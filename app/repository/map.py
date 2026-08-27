from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.context import AppContext
from app.common.enum.map import MapStatus
from app.models.map import Map
from app.models.tag import Tag


class MapRepository:
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
        dimension_x: str,
        dimension_y: str,
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
