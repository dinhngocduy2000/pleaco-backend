from typing import List, Optional
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
    ValidateOTPRequest,
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

    async def create_user(self, session: AsyncSession, user_info: User) -> UserInfo:
        session.add(user_info)  # Note: session.add() is NOT async, no await needed
        await session.flush()  # Flush to get the ID if needed
        return user_info.view()

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

    async def get_otp_code(
        self, otp_request: ValidateOTPRequest, ctx: AppContext
    ) -> str:
        try:
            return await self._redis_client.get(
                f"{settings.CACHE_OTP_CODE}:{otp_request.email}",
            )
        except Exception as e:
            logger.error(msg=f"Get OTP code repository: Exception: {e}", context=ctx)
            raise e

    async def delete_otp_code(
        self, otp_request: ValidateOTPRequest, ctx: AppContext
    ) -> None:
        try:
            await self._redis_client.delete(
                f"{settings.CACHE_OTP_CODE}:{otp_request.email}",
            )
        except Exception as e:
            logger.error(msg=f"Delete OTP code repository: Exception: {e}", context=ctx)
            raise e
