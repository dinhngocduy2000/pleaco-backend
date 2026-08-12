from sqlalchemy import UUID
from app.common.context import AppContext
from app.common.middleware.logger import Logger
from app.common.schemas.user import (
    UserCreate,
    UserInfo,
    UserQuery,
    UserUpdate,
)
from app.common.exceptions import BadRequestException
from app.models.user import User
from app.repository.registry import Registry
from sqlalchemy.ext.asyncio import AsyncSession
import bcrypt

salt = bcrypt.gensalt()
logger = Logger()


class UserService:
    repo: Registry

    def __init__(self, repo: Registry) -> None:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def create_user(self, user_create: UserCreate, ctx: AppContext) -> UserInfo:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def update_user(
        self, user_update: UserUpdate, user_id: UUID, ctx: AppContext
    ) -> UserInfo:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def get_user_by_email(self, email: str, ctx: AppContext) -> UserInfo:
        raise NotImplementedError("Pleaco-specific implementation is pending.")
