from typing import Callable
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.common.types import T
from app.external.redis.redis import RedisClient
from app.repository.user import UserRepository


class Registry:
    _redis_client: RedisClient
    _pg_engine: AsyncEngine
    _user_repo: UserRepository

    def __init__(self, pg_engine: AsyncEngine, redis_client: RedisClient) -> None:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def transaction_wrapper(self, tx_func: Callable[[AsyncSession], T]) -> T:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    def user_repo(self) -> UserRepository:
        raise NotImplementedError("Pleaco-specific implementation is pending.")
