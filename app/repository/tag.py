from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.context import AppContext
from app.models.tag import Tag


class TagRepository:
    async def get_by_group_and_name(
        self, session: AsyncSession, group_id: UUID, name: str, ctx: AppContext
    ) -> Tag | None:
        result = await session.execute(
            select(Tag).where(Tag.group_id == group_id, Tag.name == name)
        )
        return result.scalar_one_or_none()

    async def create_tag(
        self,
        session: AsyncSession,
        group_id: UUID,
        name: str,
        description: str | None,
        color: str,
        ctx: AppContext,
    ) -> Tag:
        tag = Tag(
            group_id=group_id,
            name=name,
            description=description,
            color=color,
        )
        session.add(tag)
        await session.flush()
        return tag

    async def list_by_group(
        self, session: AsyncSession, group_id: UUID, ctx: AppContext
    ) -> list[Tag]:
        stmt = (
            select(Tag)
            .where(Tag.group_id == group_id)
            .order_by(Tag.name.asc(), Tag.id.asc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_ids_and_group(
        self,
        session: AsyncSession,
        tag_ids: Sequence[UUID],
        group_id: UUID,
        ctx: AppContext,
    ) -> list[Tag]:
        if not tag_ids:
            return []
        result = await session.execute(
            select(Tag).where(Tag.id.in_(tag_ids), Tag.group_id == group_id)
        )
        return list(result.scalars().all())
