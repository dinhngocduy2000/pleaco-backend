from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.context import AppContext
from app.common.schemas.bot import BotCreateDomain, BotListQuery
from app.models.map import Map
from app.models.robot import Robot
from app.models.robot_tags import robot_tags
from app.models.tag import Tag


class BotRepository:
    async def list_bots(
        self, session: AsyncSession, query: BotListQuery, ctx: AppContext
    ) -> tuple[list[dict], int]:
        """Return a filtered page of bots and its group-scoped total."""
        columns = (
            Map.name.label("map_name"),
            Robot.name,
            Robot.serial_num,
            Robot.model,
            Robot.ip_address,
            Robot.operational_status,
            Robot.created_at,
            Robot.connection_status,
            Robot.id,
        )
        stmt = (
            select(*columns)
            .select_from(Robot)
            .outerjoin(
                Map,
                and_(Robot.map_id == Map.id, Map.group_id == Robot.group_id),
            )
        )
        count_stmt = select(func.count(func.distinct(Robot.id))).select_from(Robot)

        if query.tag_ids:
            stmt = stmt.join(robot_tags, robot_tags.c.robot_id == Robot.id)
            count_stmt = count_stmt.join(robot_tags, robot_tags.c.robot_id == Robot.id)

        filters = [Robot.group_id == query.group_id]
        if query.search is not None:
            search_pattern = f"%{query.search}%"
            filters.append(
                or_(
                    Robot.name.ilike(search_pattern),
                    Robot.serial_num.ilike(search_pattern),
                )
            )
        if query.model is not None:
            filters.append(Robot.model == query.model)
        if query.operational_status is not None:
            filters.append(Robot.operational_status == query.operational_status)
        if query.connection_status is not None:
            filters.append(Robot.connection_status == query.connection_status)
        if query.tag_ids:
            filters.append(robot_tags.c.tag_id.in_(query.tag_ids))

        stmt = (
            stmt.where(*filters)
            .order_by(Robot.created_at.desc(), Robot.id.asc())
            .offset((query.page - 1) * query.page_size)
            .limit(query.page_size)
        )
        count_stmt = count_stmt.where(*filters)

        result = await session.execute(stmt)
        total = (await session.execute(count_stmt)).scalar_one()
        return [dict(row) for row in result.mappings().all()], total

    async def get_by_group_and_serial(
        self, session: AsyncSession, group_id: UUID, serial_num: str, ctx: AppContext
    ) -> Robot | None:
        stmt = select(Robot).where(
            Robot.group_id == group_id,
            Robot.serial_num == serial_num,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id_and_group_for_update(
        self,
        session: AsyncSession,
        bot_id: UUID,
        group_id: UUID,
        ctx: AppContext,
    ) -> Robot | None:
        """Load an active-group bot with a row lock before deletion validation."""
        stmt = (
            select(Robot)
            .where(Robot.id == bot_id, Robot.group_id == group_id)
            .with_for_update()
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def hard_delete_bot(
        self,
        session: AsyncSession,
        bot_id: UUID,
        group_id: UUID,
        ctx: AppContext,
    ) -> Robot | None:
        """Hard-delete a bot scoped to its owning group."""
        stmt = (
            delete(Robot)
            .where(Robot.id == bot_id, Robot.group_id == group_id)
            .returning(Robot)
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
