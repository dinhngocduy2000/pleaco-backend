from typing import Optional

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

    async def mget(self, keys: list[str]):
        if not keys:
            return []
        async with Redis(connection_pool=self.pool) as client:
            return await client.mget(keys)

    async def record_robot_heartbeat(
        self, robot_id, message_id, received_at: str, ttl: int
    ) -> bool:
        """Atomically deduplicate a heartbeat and refresh its live timestamp.

        ``message_id`` is remembered for 24 hours. ``received_at`` is the
        backend's UTC receive time, and ``ttl`` controls how long the live
        timestamp remains available. Return ``False`` for a duplicate.
        """
        script = """
        -- KEYS[1]: message ID; KEYS[2]: live last_seen_at.
        if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
        redis.call('SET', KEYS[1], '1', 'EX', ARGV[2])
        redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[3])
        return 1
        """
        return bool(await self.eval(
            script, 2,
            f"robot:{robot_id}:heartbeat:{message_id}",
            f"robot:{robot_id}:last_seen_at",
            received_at, 86400, ttl,
        ))

    async def update_robot_status_cache(
        self, robot_id, sequence_number: int, state: str,
        connection_status: str, connection_changed: bool,
        received_at: str, ttl: int,
    ) -> bool:
        """Update cached robot status only if its sequence is newer in Redis.

        ``state`` is the JSON snapshot stored at ``robot:{robot_id}:state``.
        A transition to ``OFFLINE`` removes live freshness; a transition to
        ``ONLINE`` initializes it without replacing a later heartbeat time.
        Return ``False`` when Redis already holds this or a newer sequence.
        """
        script = """
        -- KEYS[1]: cached status; KEYS[2]: live last_seen_at.
        local old = redis.call('GET', KEYS[1])
        if old then
            local valid, decoded = pcall(cjson.decode, old)
            local sequence = valid and type(decoded) == 'table'
                and decoded.last_sequence_number
            local previous = (type(sequence) == 'number' or type(sequence) == 'string')
                and tonumber(sequence)
            if previous and previous >= tonumber(ARGV[1]) then
                return 0
            end
        end
        redis.call('SET', KEYS[1], ARGV[2])
        if ARGV[3] == '1' then
            if ARGV[4] == 'OFFLINE' then
                redis.call('DEL', KEYS[2])
            elseif ARGV[4] == 'ONLINE' then
                -- Both timestamps use the same UTC ISO format, so string order
                -- preserves time order for these backend-generated values.
                local current = redis.call('GET', KEYS[2])
                if not current or current < ARGV[5] then
                    redis.call('SET', KEYS[2], ARGV[5], 'EX', ARGV[6])
                end
            end
        end
        return 1
        """
        return bool(await self.eval(
            script, 2,
            f"robot:{robot_id}:state",
            f"robot:{robot_id}:last_seen_at",
            sequence_number, state, int(connection_changed),
            connection_status, received_at, ttl,
        ))

    async def delete(self, key):
        async with Redis(connection_pool=self.pool) as client:
            await client.delete(key)

    async def eval(self, script: str, numkeys: int, *keys_and_args):
        async with Redis(connection_pool=self.pool) as client:
            return await client.eval(script, numkeys, *keys_and_args)

    async def decrease(self, key, amount=1):
        async with Redis(connection_pool=self.pool) as client:
            await client.decr(key, amount)

    async def hset(self, key: Optional[str], mapping:Optional[dict]):
        async with Redis(connection_pool=self.pool) as client:
            await client.hset(mapping=mapping, name=key)

    async def hgetall(self, key) -> dict[str, str]:
        async with Redis(connection_pool=self.pool) as client:
            return await client.hgetall(key)

    async def hget(self, key):
        async with Redis(connection_pool=self.pool) as client:
            return await client.hget(key=key)
