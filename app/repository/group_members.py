from typing import List, Optional
from uuid import UUID

from sqlalchemy import select
from app.common.context import AppContext
from app.common.middleware.logger import Logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.external.redis.redis import RedisClient
from app.models.group_members import GroupMembers

logger = Logger()


class GroupMembersRepository:
    _redis_client: RedisClient

    def __init__(self, redis_client) -> None:
        self._redis_client = redis_client
        pass

    async def create(
        self, session: AsyncSession, group_members: List[GroupMembers], ctx: AppContext
    ) -> List[GroupMembers]:

        try:
            session.add_all(group_members)
            await session.flush()
            return group_members
        except Exception as e:
            logger.error(
                msg=f"Create group members repository: Exception: {e}", context=ctx
            )
            raise e

    async def get_group_member_by_id(
        self, session: AsyncSession, member_id: UUID, group_id: UUID
    ) -> GroupMembers:
        try:
            stmt = select(GroupMembers).where(GroupMembers.member_id == member_id)
            stmt = stmt.where(GroupMembers.group_id == group_id)
            result = await session.execute(stmt)
            group_member = result.unique().scalars().first()
            return group_member if group_member else None

        except Exception as e:
            logger.error(f"Error in getting group members: {e}")
            raise e

    # ------------ Redis ---------------
    async def set_group_member(self, member: GroupMembers) -> None:
        try:
            await self._redis_client.hset(
                key=f"{member.member_id}:{member.group_id}",
                mapping={
                    "member_id": str(member.member_id),
                    "group_id": str(member.group_id),
                    "role": member.role.value,  # Enum → str
                    "created_at": member.created_at.isoformat(),
                    "updated_at": member.updated_at.isoformat(),  # datetime → str
                    # datetime → str
                },
            )
            return
        except Exception as e:
            logger.error(msg=f"Error in setting group member in Redis: {e}")
            raise e

    async def get_group_member_redis(
        self, group_id: UUID, member_id: UUID
    ) -> dict[str, str]:
        try:
            group_member = await self._redis_client.hgetall(f"{member_id}:{group_id}")
            return group_member
        except Exception as e:
            logger.error(msg=f"Error in getting group member in Redis: {e}")
            raise e
