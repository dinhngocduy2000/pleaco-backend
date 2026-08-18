from typing import Dict, List, Optional
from uuid import UUID
from sqlalchemy import Select, and_, select, update
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.context import AppContext
from app.common.enum.group_member_status import GroupMemberInvitationStatus
from app.common.middleware.logger import Logger
from app.common.schemas.group import (
    GroupCreateDomain,
    GroupJoinOption,
    GroupQuery,
    GroupUpdate,
)
from app.external.redis.redis import RedisClient
from app.models.group import Group
from app.models.group_members import GroupMembers
from app.models.user import User

logger = Logger()


class GroupRepository:
    _redis_client: RedisClient

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis_client = redis_client

    def _prepare_query(
        self, query: GroupQuery, stmt: Select, options: Optional[GroupJoinOption] = None
    ) -> Select:
        if query.id is not None:
            stmt = stmt.where(Group.id == query.id)
        if query.name is not None:
            stmt = stmt.where(Group.name == query.name)
        if query.owner_id is not None:
            stmt = stmt.where(Group.owner_id == query.owner_id)
        if query.members is not None and len(query.members) > 0:
            stmt = stmt.where(
                Group.members.any(
                    and_(
                        GroupMembers.member_id.in_(query.members),
                        GroupMembers.invitation_status
                        == GroupMemberInvitationStatus.ACCEPTED,
                    )
                )
            )
        if options is not None:
            if options.include_members:
                stmt = stmt.options(
                    joinedload(Group.members).joinedload(GroupMembers.user)
                )
        return stmt

    async def create_group(
        self, session: AsyncSession, group_create: GroupCreateDomain, ctx: AppContext
    ) -> Group:
        try:
            new_group = Group()
            new_group.name = group_create.name
            new_group.description = group_create.description
            new_group.owner_id = group_create.owner_id
            session.add(new_group)
            await session.flush()

            return new_group
        except Exception as e:
            logger.error(msg=f"Create group repository: Exception: {e}", context=ctx)
            raise e

    async def get_group(
        self,
        session: AsyncSession,
        query: GroupQuery,
        ctx: AppContext,
        options: Optional[GroupJoinOption] = None,
    ) -> Optional[Group]:

        try:
            stmt = select(Group)
            stmt = self._prepare_query(query, stmt, options)
            result = await session.execute(stmt)
            group = result.unique().scalars().first()
            return group if group else None
        except Exception as e:
            logger.error(msg=f"Get group repository: Exception: {e}", context=ctx)
            raise e

    async def list_groups(
        self,
        session: AsyncSession,
        query: GroupQuery,
        ctx: AppContext,
        options: Optional[GroupJoinOption] = None,
    ) -> List[Group]:
        try:
            stmt = select(Group)
            stmt = self._prepare_query(query, stmt, options)
            result = await session.execute(stmt)
            groups = result.scalars().all()
            return groups
        except Exception as e:
            logger.error(msg=f"List groups repository: Exception: {e}", context=ctx)
            raise e

    async def list_member_users(
        self, session: AsyncSession, group_id: UUID, ctx: AppContext
    ) -> List[User]:
        try:
            stmt = (
                select(User)
                .join(GroupMembers, GroupMembers.member_id == User.id)
                .where(
                    GroupMembers.group_id == group_id,
                    GroupMembers.invitation_status
                    == GroupMemberInvitationStatus.ACCEPTED,
                )
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())
        except Exception as e:
            logger.error(
                msg=f"List group member users repository: Exception: {e}",
                context=ctx,
            )
            raise

    async def list_group_map(
        self, session: AsyncSession, query: GroupQuery, ctx: AppContext
    ) -> List[Dict[UUID, str]]:
        try:
            stmt = select(Group.id, Group.name)
            stmt = self._prepare_query(query, stmt)
            result = await session.execute(stmt)
            groups = result.all()
            return [{"id": group[0], "name": group[1]} for group in groups]
        except Exception as e:
            logger.error(msg=f"List groups repository: Exception: {e}", context=ctx)
            raise e

    async def update_group(
        self, session: AsyncSession, group_update: GroupUpdate, ctx: AppContext
    ) -> Group:
        try:
            stmt = update(Group).where(Group.id == group_update.id)
            stmt = stmt.values(
                group_update.model_dump(mode="python", exclude_none=True)
            )
            await session.execute(stmt)
            await session.flush()
            return group_update
        except Exception as e:
            logger.error(msg=f"Update group repository: Exception: {e}", context=ctx)
            raise e

    # --------------- Redis -----------------------

    async def set_group_owner(
        self, group_owner: UUID, group_id: UUID, ctx: AppContext
    ) -> None:
        try:
            await self._redis_client.set(key=str(group_id), value=str(group_owner))
            return
        except Exception as e:
            logger.error(msg=f"Error setting group owner in Redis: {e}", context=ctx)
            raise e

    async def get_group_owner(self, group_id: UUID, ctx: AppContext) -> Optional[UUID]:
        try:
            owner = await self._redis_client.get(group_id)
            return owner

        except Exception as e:
            logger.error(msg=f"Error getting group owner: {e}", context=ctx)
            raise e
