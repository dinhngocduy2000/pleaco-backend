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
        self._pg_engine = pg_engine
        self._user_repo = UserRepository(redis_client=redis_client)
        self._redis_client = redis_client

    async def transaction_wrapper(self, tx_func: Callable[[AsyncSession], T]) -> T:
        try:
            async_session = async_sessionmaker(self._pg_engine, expire_on_commit=False)
            session = async_session()
            await session.begin()
            res = await tx_func(session)
            await session.commit()
            return res
        except Exception as e:
            if session is not None and session.is_active:
                await session.rollback()
            raise e
        finally:
            if session is not None and session.is_active:
                await session.close()

    # def user_repo(self) -> UserRepository:
    #     return self._user_repo
