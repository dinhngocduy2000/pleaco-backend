from collections import defaultdict
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.context import AppContext
from app.models.map import Map
from app.models.map_tags import map_tags
from app.models.tag import Tag


class MapTagsRepository:
    async def get_by_map_ids(
        self,
        session: AsyncSession,
        map_ids: Sequence[UUID],
        group_id: UUID,
        ctx: AppContext,
    ) -> dict[UUID, list[Tag]]:
        """Return group-scoped tags keyed by map ID."""
        if not map_ids:
            return {}

        stmt = (
            select(map_tags.c.map_id, Tag)
            .join(Map, Map.id == map_tags.c.map_id)
            .join(Tag, Tag.id == map_tags.c.tag_id)
            .where(
                map_tags.c.map_id.in_(map_ids),
                Map.group_id == group_id,
                Tag.group_id == group_id,
            )
            .order_by(map_tags.c.map_id.asc(), Tag.name.asc(), Tag.id.asc())
        )
        result = await session.execute(stmt)
        tags_by_map: dict[UUID, list[Tag]] = defaultdict(list)
        for map_id, tag in result.all():
            tags_by_map[map_id].append(tag)
        return dict(tags_by_map)
