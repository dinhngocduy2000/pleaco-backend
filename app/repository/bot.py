from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.context import AppContext
from app.common.schemas.bot import BotCreateDomain
from app.models.robot import Robot
from app.models.tag import Tag


class BotRepository:
    async def get_by_group_and_serial(
        self, session: AsyncSession, group_id: UUID, serial_num: str, ctx: AppContext
    ) -> Robot | None:
        stmt = select(Robot).where(
            Robot.group_id == group_id,
            Robot.serial_num == serial_num,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_tags_by_ids(
        self, session: AsyncSession, tag_ids: Sequence[UUID], ctx: AppContext
    ) -> list[Tag]:
        if not tag_ids:
            return []
        result = await session.execute(select(Tag).where(Tag.id.in_(tag_ids)))
        return list(result.scalars().all())

    async def create_bot(
        self,
        session: AsyncSession,
        bot_create: BotCreateDomain,
        tags: Sequence[Tag],
        ctx: AppContext,
    ) -> Robot:
        bot = Robot(
            group_id=bot_create.group_id,
            map_id=bot_create.map_id,
            name=bot_create.name,
            serial_num=bot_create.serial_num,
            model=bot_create.model,
            ip_address=bot_create.ip_address,
            connection_status=bot_create.connection_status,
            operational_status=bot_create.operational_status,
        )
        bot.tags = list(tags)
        session.add(bot)
        await session.flush()
        return bot
