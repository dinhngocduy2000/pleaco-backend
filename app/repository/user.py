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
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    def _prepare_query(self, query: UserQuery, stmt: Select) -> Select:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def create_user(self, session: AsyncSession, user_info: User) -> UserInfo:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def get_list_users(self, session: AsyncSession) -> List[str]:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def update_user(
        self,
        session: AsyncSession,
        user_id: UUID,
        user_update: UserUpdate,
        ctx: AppContext,
    ) -> None:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def delete_user(self, session: AsyncSession, user_id: str) -> None:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def get_user_by_id(self, session: AsyncSession, user_id: str) -> None:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def get(
        self,
        session: AsyncSession,
        query: UserQuery,
        ctx: AppContext,
        options: Optional[UserJoinOption] = None,
    ) -> Optional[User]:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    # ---------------- Redis ----------------

    async def set_hashed_token(
        self,
        hashed_token: str,
        ctx: AppContext,
        expire: Optional[int] = settings.ACCESS_TOKEN_EXPIRE_SECONDS,
    ) -> None:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def get_token(self, hashed_token: str, ctx: AppContext) -> str:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def delete_token(self, hashed_token: str, ctx: AppContext) -> None:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def set_otp_code(self, email: str, otp_code: str, ctx: AppContext) -> None:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def get_otp_code(
        self, otp_request: ValidateOTPRequest, ctx: AppContext
    ) -> str:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def delete_otp_code(
        self, otp_request: ValidateOTPRequest, ctx: AppContext
    ) -> None:
        raise NotImplementedError("Pleaco-specific implementation is pending.")
