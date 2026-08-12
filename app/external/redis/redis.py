from redis.asyncio import ConnectionPool, Redis
from app.common.middleware.logger import Logger
from app.core.config import settings

logger = Logger()


class RedisClient:
    pool: ConnectionPool

    def __init__(self):
        self.pool = ConnectionPool(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=0,
            password=settings.REDIS_PASSWORD,
            max_connections=20,
            decode_responses=True,
            socket_connect_timeout=10,
            socket_timeout=10,
            username="default",
        )

    async def set(self, key, value, expire=None):
        async with Redis(connection_pool=self.pool) as client:
            await client.set(key, value, ex=expire)

    async def get(self, key):
        async with Redis(connection_pool=self.pool) as client:
            return await client.get(key)

    async def delete(self, key):
        async with Redis(connection_pool=self.pool) as client:
            await client.delete(key)

    async def decrease(self, key, amount=1):
        async with Redis(connection_pool=self.pool) as client:
            await client.decr(key, amount)

    async def hset(self, key, mapping):
        async with Redis(connection_pool=self.pool) as client:
            await client.hset(key=key, mapping=mapping)

    async def hgetall(self, key) -> dict[str, str]:
        async with Redis(connection_pool=self.pool) as client:
            return await client.hgetall(key)

    async def hget(self, key):
        async with Redis(connection_pool=self.pool) as client:
            return await client.hget(key=key)
