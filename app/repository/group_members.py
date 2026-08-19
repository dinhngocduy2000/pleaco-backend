import datetime
from typing import List, Optional, Sequence
from uuid import UUID

from sqlalchemy import Select, delete, func, select, update
from app.common.context import AppContext
from app.common.enum.group_member_status import GroupMemberInvitationStatus
from app.common.enum.user_roles import GroupRole
from app.common.middleware.logger import Logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.external.redis.redis import RedisClient
from app.models.group_members import GroupMembers
from app.models.user import User
from app.common.enum.user_status import UserStatus
from app.common.schemas.group import GroupInvitationInfo, GroupMemberListQuery

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
        self,
        session: AsyncSession,
        member_id: UUID,
        group_id: UUID,
        accepted_only: bool = False,
    ) -> GroupMembers | None:
        try:
            stmt = select(GroupMembers).where(GroupMembers.member_id == member_id)
            stmt = stmt.where(GroupMembers.group_id == group_id)
            if accepted_only:
                stmt = stmt.where(
                    GroupMembers.invitation_status
                    == GroupMemberInvitationStatus.ACCEPTED
                )
            result = await session.execute(stmt)
            group_member = result.unique().scalars().first()
            return group_member if group_member else None

        except Exception as e:
            logger.error(f"Error in getting group members: {e}")
            raise e

    async def update_group_member_role(
        self,
        session: AsyncSession,
        member_id: UUID,
        group_id: UUID,
        role: GroupRole,
        ctx: AppContext,
    ) -> GroupMembers | None:
        """Update the role of an accepted group membership."""
        try:
            stmt = (
                update(GroupMembers)
                .where(
                    GroupMembers.member_id == member_id,
                    GroupMembers.group_id == group_id,
                    GroupMembers.invitation_status
                    == GroupMemberInvitationStatus.ACCEPTED,
                )
                .values(role=role)
                .returning(GroupMembers)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(
                msg=f"Update group member role repository: Exception: {e}",
                context=ctx,
            )
            raise

    async def hard_delete_group_member(
        self,
        session: AsyncSession,
        member_id: UUID,
        group_id: UUID,
        ctx: AppContext,
    ) -> GroupMembers | None:
        """Hard-delete a membership row regardless of invitation status."""
        try:
            stmt = (
                delete(GroupMembers)
                .where(
                    GroupMembers.member_id == member_id,
                    GroupMembers.group_id == group_id,
                )
                .returning(GroupMembers)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(
                msg=f"Hard delete group member repository: Exception: {e}",
                context=ctx,
            )
            raise

    async def list_group_members_by_ids(
        self,
        session: AsyncSession,
        group_id: UUID,
        member_ids: Sequence[UUID],
        ctx: AppContext,
    ) -> dict[UUID, GroupMembers]:
        """Return all membership rows for the requested users in a group."""
        if not member_ids:
            return {}
        try:
            stmt = select(GroupMembers).where(
                GroupMembers.group_id == group_id,
                GroupMembers.member_id.in_(member_ids),
            )
            result = await session.execute(stmt)
            members = result.scalars().all()
            return {member.member_id: member for member in members}
        except Exception as e:
            logger.error(
                msg=f"List group memberships repository: Exception: {e}",
                context=ctx,
            )
            raise

    async def list_existing_member_ids(
        self,
        session: AsyncSession,
        group_id: UUID,
        member_ids: Sequence[UUID],
        ctx: AppContext,
    ) -> set[UUID]:
        """Return requested users that are already members of ``group_id``."""
        try:
            if not member_ids:
                return set()
            stmt = select(GroupMembers.member_id).where(
                GroupMembers.group_id == group_id,
                GroupMembers.member_id.in_(member_ids),
                GroupMembers.invitation_status == GroupMemberInvitationStatus.ACCEPTED,
            )
            result = await session.execute(stmt)
            return set(result.scalars().all())
        except Exception as e:
            logger.error(
                msg=f"List existing group members repository: Exception: {e}",
                context=ctx,
            )
            raise

    async def upsert_pending_invitations(
        self,
        session: AsyncSession,
        invitations: Sequence[GroupInvitationInfo],
        existing_members: dict[UUID, GroupMembers],
        ctx: AppContext,
    ) -> list[GroupMembers]:
        """Create or refresh pending membership rows for validated invitations."""
        pending_members: list[GroupMembers] = []
        try:
            for invitation in invitations:
                member = existing_members.get(invitation.member_id)
                if member is None:
                    member = GroupMembers(
                        member_id=invitation.member_id,
                        group_id=invitation.group_id,
                    )
                    session.add(member)
                member.role = invitation.role
                member.invitation_status = GroupMemberInvitationStatus.PENDING
                member.invitation_id = invitation.invitation_id
                member.invitation_expires_at = invitation.expires_at
                pending_members.append(member)
            await session.flush()
            return pending_members
        except Exception as e:
            logger.error(
                msg=f"Upsert pending group invitations repository: Exception: {e}",
                context=ctx,
            )
            raise

    async def accept_pending_invitation(
        self,
        session: AsyncSession,
        invitation_id: UUID,
        group_id: UUID,
        member_id: UUID,
        now: datetime.datetime,
        ctx: AppContext,
    ) -> GroupMembers | None:
        """Atomically accept a still-valid pending membership invitation."""
        try:
            stmt = (
                update(GroupMembers)
                .where(
                    GroupMembers.member_id == member_id,
                    GroupMembers.group_id == group_id,
                    GroupMembers.invitation_id == invitation_id,
                    GroupMembers.invitation_status
                    == GroupMemberInvitationStatus.PENDING,
                    GroupMembers.invitation_expires_at > now,
                )
                .values(
                    invitation_status=GroupMemberInvitationStatus.ACCEPTED,
                    invitation_id=None,
                    invitation_expires_at=None,
                )
                .returning(GroupMembers)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(
                msg=f"Accept pending group invitation repository: Exception: {e}",
                context=ctx,
            )
            raise

    async def reject_expired_invitations(
        self, session: AsyncSession, now: datetime.datetime, ctx: AppContext
    ) -> int:
        """Mark due pending memberships rejected in an idempotent bulk update."""
        try:
            stmt = (
                update(GroupMembers)
                .where(
                    GroupMembers.invitation_status
                    == GroupMemberInvitationStatus.PENDING,
                    GroupMembers.invitation_expires_at <= now,
                )
                .values(
                    invitation_status=GroupMemberInvitationStatus.REJECTED,
                    invitation_id=None,
                    invitation_expires_at=None,
                )
            )
            result = await session.execute(stmt)
            return result.rowcount or 0
        except Exception as e:
            logger.error(
                msg=f"Reject expired group invitations repository: Exception: {e}",
                context=ctx,
            )
            raise

    def _prepare_list_query(self, query: GroupMemberListQuery, stmt: Select) -> Select:
        stmt = stmt.where(
            GroupMembers.group_id == query.group_id,
            # GroupMembers.invitation_status == GroupMemberInvitationStatus.ACCEPTED,
            User.status != UserStatus.DELETED,
        )
        if query.email is not None:
            stmt = stmt.where(User.email.ilike(f"%{query.email}%"))
        if query.role is not None:
            stmt = stmt.where(GroupMembers.role == query.role)
        if query.status is not None:
            stmt = stmt.where(User.status == query.status)

        order_column = (
            User.name if query.order_by.value == "name" else GroupMembers.created_at
        )
        order = (
            order_column.asc()
            if query.order_direction.value == "asc"
            else order_column.desc()
        )
        return stmt.order_by(order, GroupMembers.member_id.asc())

    async def list_group_members(
        self,
        session: AsyncSession,
        query: GroupMemberListQuery,
        ctx: AppContext,
    ) -> tuple[list[tuple[GroupMembers, User]], int]:
        """Return a filtered page of non-deleted group members and its total."""
        try:
            base_stmt = select(GroupMembers, User).join(
                User, GroupMembers.member_id == User.id
            )
            stmt = self._prepare_list_query(query, base_stmt)
            stmt = stmt.offset((query.page - 1) * query.page_size).limit(
                query.page_size
            )
            result = await session.execute(stmt)

            count_stmt = (
                select(func.count())
                .select_from(GroupMembers)
                .join(User, GroupMembers.member_id == User.id)
            )
            count_stmt = self._prepare_list_query(query, count_stmt).order_by(None)
            total = (await session.execute(count_stmt)).scalar_one()
            return list(result.all()), total
        except Exception as e:
            logger.error(
                msg=f"List group members repository: Exception: {e}", context=ctx
            )
            raise

    # ------------ Redis ---------------
    async def set_group_member_redis(
        self, member: GroupMembers, ctx: AppContext
    ) -> None:
        logger.info(
            msg=f"Setting group member in Redis: {member.__dict__}", context=ctx
        )
        try:
            await self._redis_client.hset(
                key=f"{member.member_id}:{member.group_id}",
                mapping={
                    "member_id": str(member.member_id),
                    "group_id": str(member.group_id),
                    "role": member.role.value,  # Enum → str
                    "created_at": datetime.datetime.now().isoformat(),  # datetime → str
                    "updated_at": datetime.datetime.now().isoformat(),  # datetime → str
                    # datetime → str
                },
            )
            return
        except Exception as e:
            logger.error(
                msg=f"Error in setting group member in Redis: {e}", context=ctx
            )
            raise e

    async def get_group_member_redis(
        self, group_id: UUID, member_id: UUID, ctx: AppContext
    ) -> dict[str, str]:
        try:
            group_member = await self._redis_client.hgetall(f"{member_id}:{group_id}")
            return group_member
        except Exception as e:
            logger.error(
                msg=f"Error in getting group member in Redis: {e}", context=ctx
            )
            raise e

    async def delete_group_member_redis(
        self, group_id: UUID, member_id: UUID, ctx: AppContext
    ) -> None:
        """Evict the cached membership after a hard delete."""
        try:
            await self._redis_client.delete(f"{member_id}:{group_id}")
        except Exception as e:
            logger.error(
                msg=f"Error deleting group member from Redis: {e}", context=ctx
            )
            raise
