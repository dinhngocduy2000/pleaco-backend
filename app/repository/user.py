from typing import List, Optional
from uuid import UUID as PythonUUID
from sqlalchemy import UUID, Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.context import AppContext
from app.common.enum.user_status import UserStatus
from app.common.middleware.logger import Logger
from app.common.schemas.user import (
    UserInfo,
    UserJoinOption,
    UserQuery,
    UserUpdate,
)
from app.external.redis.redis import RedisClient
from app.models.user import User
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
                self._user_profile_cache_key(user_profile.id),
                user_profile.model_dump_json(),
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
