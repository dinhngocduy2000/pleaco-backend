"""Atomic command reservations, transitions, and confirmed position writes."""

import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import cast
from uuid import UUID

from app.external.redis.redis import RedisClient
from app.common.schemas.robot_action import RobotCommandRecord, RobotPositionRecord


class RobotCommandStore:
    ttl = 43200

    def __init__(self, redis_client: RedisClient) -> None:
        self.redis_client = redis_client

    @staticmethod
    def active_key(robot_id: UUID | str) -> str:
        return f"robot:{robot_id}:active_command"

    @staticmethod
    def command_key(robot_id: UUID | str, command_id: UUID | str) -> str:
        return f"robot:{robot_id}:command:{command_id}"

    @staticmethod
    def position_key(robot_id: UUID | str) -> str:
        return f"robot:{robot_id}:position"

    @staticmethod
    def encode(value: RobotCommandRecord | RobotPositionRecord) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    async def get(self, robot_id: UUID | str, command_id: UUID | str) -> RobotCommandRecord | None:
        raw = await self.redis_client.get(self.command_key(robot_id, command_id))
        return cast(RobotCommandRecord, json.loads(raw)) if raw else None

    async def active(self, robot_id: UUID | str) -> RobotCommandRecord | None:
        command_id = await self.redis_client.get(self.active_key(robot_id))
        if not command_id:
            return None
        record = await self.get(robot_id, command_id)
        if record is None:
            # allkeys-lru may evict a record before its reservation. Release
            # only that dangling reservation, never a concurrent newer one.
            await self.redis_client.eval("""
                if redis.call('GET', KEYS[1]) == ARGV[1]
                   and redis.call('EXISTS', KEYS[2]) == 0 then
                    return redis.call('DEL', KEYS[1])
                end
                return 0
            """, 2, self.active_key(robot_id), self.command_key(robot_id, command_id), command_id)
        return record

    async def position(self, robot_id: UUID | str) -> RobotPositionRecord | None:
        raw = await self.redis_client.get(self.position_key(robot_id))
        return cast(RobotPositionRecord, json.loads(raw)) if raw else None

    async def reserve(self, record: RobotCommandRecord, position: RobotPositionRecord | None) -> bool:
        script = """
        if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
        local position = redis.call('GET', KEYS[3]) or ''
        if position ~= ARGV[3] then return 0 end
        redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[4])
        redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[4])
        return 1
        """
        return bool(await self.redis_client.eval(
            script, 3, self.active_key(record["robot_id"]),
            self.command_key(record["robot_id"], record["command_id"]),
            self.position_key(record["robot_id"]),
            record["command_id"], self.encode(record),
            self.encode(position) if position is not None else "", self.ttl,
        ))

    async def transition(
        self, previous: RobotCommandRecord, updated: RobotCommandRecord, *,
        terminal: bool = False, position: RobotPositionRecord | None = None,
    ) -> bool:
        # Full-record CAS rejects duplicates and concurrent timeout/retry/ACK races.
        script = """
        if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
        if redis.call('GET', KEYS[2]) ~= ARGV[2] then return 0 end
        if ARGV[7] == '1' then
            local now = redis.call('TIME')
            if tonumber(now[1]) + tonumber(now[2]) / 1000000 >= tonumber(ARGV[8]) then return 0 end
        end
        redis.call('SET', KEYS[2], ARGV[3], 'EX', ARGV[4])
        if ARGV[5] == '1' then
            redis.call('DEL', KEYS[1])
        else
            redis.call('EXPIRE', KEYS[1], ARGV[4])
        end
        if ARGV[6] ~= '' then
            redis.call('SET', KEYS[3], ARGV[6], 'EX', ARGV[4])
        end
        return 1
        """
        return bool(await self.redis_client.eval(
            script, 3, self.active_key(previous["robot_id"]),
            self.command_key(previous["robot_id"], previous["command_id"]),
            self.position_key(previous["robot_id"]), previous["command_id"],
            self.encode(previous), self.encode(updated), self.ttl, int(terminal),
            self.encode(position) if position is not None else "",
            int(updated["status"] in {"QUEUED", "AWAITING_ACK", "SUCCEEDED"}),
            datetime.fromisoformat(previous["deadline"]).timestamp(),
        ))

    def dispatch_key(self, record: RobotCommandRecord) -> str:
        return self.command_key(record["robot_id"], record["command_id"]) + f":dispatch:{record['attempt']}"

    async def dispatched(self, record: RobotCommandRecord) -> bool:
        return bool(await self.redis_client.get(self.dispatch_key(record)))

    async def mark_dispatched(self, record: RobotCommandRecord) -> None:
        await self.redis_client.set(self.dispatch_key(record), "1", expire=self.ttl)

    async def active_robot_ids(self) -> AsyncIterator[UUID]:
        async for key in self.redis_client.scan_iter("robot:*:active_command"):
            try:
                yield UUID(key.split(":")[1])
            except (ValueError, IndexError):
                continue
