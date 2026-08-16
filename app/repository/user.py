from typing import List, Optional, Sequence
from uuid import UUID as PythonUUID
from sqlalchemy import UUID, Select, and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.context import AppContext
from app.common.enum.user_status import UserStatus
from app.common.middleware.logger import Logger
from app.common.schemas.common import HashMapResponse
from app.common.schemas.user import (
    UserInfo,
    UserJoinOption,
    UserProfileGroupInfo,
    UserQuery,
    UserUpdate,
)
from app.external.redis.redis import RedisClient
from app.models.group_members import GroupMembers
from app.models.user import User
from app.models.group import Group
from app.core.config import settings

logger = Logger()


class UserRepository:
    _redis_client: RedisClient

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis_client = redis_client

    def _prepare_query(self, query: UserQuery, stmt: Select) -> Select:
        stmt = stmt.where(User.status != UserStatus.DELETED)

        if query.id is not None:
            stmt = stmt.where(User.id == query.id)
        if query.email is not None:
            stmt = stmt.where(User.email == query.email)
        if query.name is not None:
            stmt = stmt.where(User.name == query.name)
        if query.status is not None:
            stmt = stmt.where(User.status == query.status)

        return stmt

    async def save_user(self, session: AsyncSession, user: User) -> User:
        """Persist a new or modified user in the caller's transaction."""
        session.add(user)
        await session.flush()
        return user

    async def get_list_users(self, session: AsyncSession) -> List[str]:
        pass

    async def update_user(
        self,
        session: AsyncSession,
        user_id: UUID,
        user_update: UserUpdate,
        ctx: AppContext,
    ) -> None:
        try:
            user_update_data = user_update.model_dump(mode="python", exclude_none=True)
            if user_update_data is not None:
                stmt = update(User).where(User.id == user_id)
                stmt = stmt.values(user_update_data)
            await session.execute(stmt)
            await session.flush()
        except Exception as e:
            logger.error(msg=f"Update user repository: Exception: {e}", context=ctx)
            raise e
        return

    async def delete_user(self, session: AsyncSession, user_id: str) -> None:
        pass

    async def get_user_by_id(self, session: AsyncSession, user_id: str) -> None:
        pass

    async def get(
        self,
        session: AsyncSession,
        query: UserQuery,
        ctx: AppContext,
        options: Optional[UserJoinOption] = None,
    ) -> Optional[User]:
        try:
            stmt = select(User)
            stmt = self._prepare_query(query, stmt)

            result = await session.execute(stmt)
            user = result.scalars().first()
            return user if user else None
        except Exception as e:
            logger.error(msg=f"Get user repository: Exception: {e}", context=ctx)
            raise e

    async def get_by_emails(
        self, session: AsyncSession, emails: Sequence[str], ctx: AppContext
    ) -> List[User]:
        """Return non-deleted users matching normalized email addresses."""
        try:
            if not emails:
                return []
            stmt = select(User).where(User.status != UserStatus.DELETED)
            stmt = stmt.where(func.lower(User.email).in_(emails))
            result = await session.execute(stmt)
            return list(result.scalars().all())
        except Exception as e:
            logger.error(
                msg=f"Get users by emails repository: Exception: {e}", context=ctx
            )
            raise

    async def get_user_profile(
        self,
        session: AsyncSession,
        user_id: PythonUUID,
        ctx: AppContext,
        options: UserJoinOption | None = None,
    ) -> UserInfo | None:
        try:
            include_group = (
                options is not None and options.included_owned_groups is True
            )
            if include_group:
                stmt = (
                    select(User, Group, GroupMembers)
                    .outerjoin(Group, User.active_group_id == Group.id)
                    .outerjoin(
                        GroupMembers,
                        and_(
                            GroupMembers.group_id == Group.id,
                            GroupMembers.member_id == User.id,
                        ),
                    )
                    .where(User.id == user_id, User.status != UserStatus.DELETED)
                )
                result = await session.execute(stmt)
                row = result.first()
                if row is None:
                    return None
                user, group, member = row
            else:
                user = await self.get(
                    session=session,
                    query=UserQuery(id=user_id),
                    ctx=ctx,
                    options=options,
                )
                group = None

            if user is None:
                return None
            return UserInfo(
                id=user.id,
                name=user.name,
                email=user.email,
                status=user.status,
                created_at=user.created_at,
                updated_at=user.updated_at,
                image_url=user.image_url,
                group_id=user.active_group_id,
                group=(
                    UserProfileGroupInfo(
                        value=group.id,
                        label=group.name,
                        role=member.role if member is not None else None,
                    )
                    if group is not None
                    else None
                ),
            )
        except Exception as e:
            logger.error(
                msg=f"Get user profile repository: Exception: {e}", context=ctx
            )
            raise

    async def get_user_profile_with_cache(
        self,
        session: AsyncSession,
        user_id: PythonUUID,
        ctx: AppContext,
        options: UserJoinOption | None = None,
    ) -> UserInfo | None:
        """Load a user profile from Redis, falling back to PostgreSQL on a miss.

        Group-inclusive profiles bypass the cache because the cached profile does
        not contain the optional active-group projection.
        """
        include_group = options is not None and options.included_owned_groups is True
        if not include_group:
            cached_profile = await self.get_cached_user_profile(user_id, ctx)
            if cached_profile is not None:
                logger.info(msg="User profile found in cache", context=ctx)
                return cached_profile

            logger.info(
                msg="User profile not found in cache, fetching from DB...",
                context=ctx,
            )

        user_profile = await self.get_user_profile(
            session=session,
            user_id=user_id,
            ctx=ctx,
            options=options,
        )
        if user_profile is not None and not include_group:
            logger.info(
                msg="User profile fetched from DB, writing to redis cache...",
                context=ctx,
            )
            await self.set_cached_user_profile(user_profile, ctx)
        return user_profile

    # ---------------- Redis ----------------

    @staticmethod
    def _user_profile_cache_key(user_id: PythonUUID) -> str:
        return f"user-profile:{user_id}"

    async def get_cached_user_profile(
        self, user_id: PythonUUID, ctx: AppContext
    ) -> UserInfo | None:
        try:
            cached_profile = await self._redis_client.get(
                self._user_profile_cache_key(user_id)
            )
            if cached_profile is None:
                return None
            return UserInfo.model_validate_json(cached_profile)
        except Exception as e:
            logger.error(
                msg=f"Get cached user profile repository: Exception: {e}",
                context=ctx,
            )
            raise

    async def set_cached_user_profile(
        self, user_profile: UserInfo, ctx: AppContext
    ) -> None:
        try:
            await self._redis_client.set(
                key=self._user_profile_cache_key(user_profile.id),
                value=user_profile.model_dump_json(),
                expire=settings.ACCESS_TOKEN_EXPIRE_SECONDS,
            )
        except Exception as e:
            logger.error(
                msg=f"Set cached user profile repository: Exception: {e}",
                context=ctx,
            )
            raise

    async def set_hashed_token(
        self,
        hashed_token: str,
        ctx: AppContext,
        expire: Optional[int] = settings.ACCESS_TOKEN_EXPIRE_SECONDS,
    ) -> None:
        try:
            await self._redis_client.set(
                f"{settings.cache_token_hash}:{hashed_token}",
                hashed_token,
                expire=expire,
            )
        except Exception as e:
            logger.error(
                msg=f"Set hashed token repository: Exception: {e}", context=ctx
            )
            raise e

    async def get_token(self, hashed_token: str, ctx: AppContext) -> str:
        try:
            return await self._redis_client.get(
                f"{settings.cache_token_hash}:{hashed_token}"
            )
        except Exception as e:
            logger.error(msg=f"Get token repository: Exception: {e}", context=ctx)
            raise e

    async def delete_token(self, hashed_token: str, ctx: AppContext) -> None:
        try:
            await self._redis_client.delete(
                f"{settings.cache_token_hash}:{hashed_token}"
            )
        except Exception as e:
            logger.error(msg=f"Delete token repository: Exception: {e}", context=ctx)
            raise e

    async def set_otp_code(self, email: str, otp_code: str, ctx: AppContext) -> None:
        try:
            await self._redis_client.set(
                f"{settings.CACHE_OTP_CODE}:{email}",
                otp_code,
                expire=settings.OTP_CODE_EXPIRE_SECONDS,
            )
        except Exception as e:
            logger.error(msg=f"Set OTP code repository: Exception: {e}", context=ctx)
            raise e

    async def get_otp_code(self, email: str, ctx: AppContext) -> str | None:
        try:
            return await self._redis_client.get(
                f"{settings.CACHE_OTP_CODE}:{email}",
            )
        except Exception as e:
            logger.error(msg=f"Get OTP code repository: Exception: {e}", context=ctx)
            raise e

    async def delete_otp_code(self, email: str, ctx: AppContext) -> None:
        try:
            await self._redis_client.delete(
                f"{settings.CACHE_OTP_CODE}:{email}",
            )
        except Exception as e:
            logger.error(msg=f"Delete OTP code repository: Exception: {e}", context=ctx)
            raise e
