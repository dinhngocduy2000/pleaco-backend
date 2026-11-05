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
        saved_command = await self.redis_client.get(self.command_key(robot_id, command_id))
        return cast(RobotCommandRecord, json.loads(saved_command)) if saved_command else None

    async def active(self, robot_id: UUID | str) -> RobotCommandRecord | None:
        command_id = await self.redis_client.get(self.active_key(robot_id))
        if not command_id:
            return None
        active_command = await self.get(robot_id, command_id)
        if active_command is None:
            # allkeys-lru may evict a record before its reservation. Release
            # only that dangling reservation, never a concurrent newer one.
            await self.redis_client.eval("""
                if redis.call('GET', KEYS[1]) == ARGV[1]
                   and redis.call('EXISTS', KEYS[2]) == 0 then
                    return redis.call('DEL', KEYS[1])
                end
                return 0
            """, 2, self.active_key(robot_id), self.command_key(robot_id, command_id), command_id)
        return active_command

    async def position(self, robot_id: UUID | str) -> RobotPositionRecord | None:
        saved_position = await self.redis_client.get(self.position_key(robot_id))
        return cast(RobotPositionRecord, json.loads(saved_position)) if saved_position else None

    async def reserve(self, command: RobotCommandRecord, position: RobotPositionRecord | None) -> bool:
        reserve_script = """
        if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
        local position = redis.call('GET', KEYS[3]) or ''
        if position ~= ARGV[3] then return 0 end
        redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[4])
        redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[4])
        return 1
        """
        return bool(await self.redis_client.eval(
            reserve_script, 3, self.active_key(command["robot_id"]),
            self.command_key(command["robot_id"], command["command_id"]),
            self.position_key(command["robot_id"]),
            command["command_id"], self.encode(command),
            self.encode(position) if position is not None else "", self.ttl,
        ))

    async def transition(
        self, current_command: RobotCommandRecord, updated_command: RobotCommandRecord, *,
        terminal: bool = False, position: RobotPositionRecord | None = None,
    ) -> bool:
        # Full-record CAS rejects duplicates and concurrent timeout/retry/ACK races.
        transition_script = """
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
            transition_script, 3, self.active_key(current_command["robot_id"]),
            self.command_key(current_command["robot_id"], current_command["command_id"]),
            self.position_key(current_command["robot_id"]), current_command["command_id"],
            self.encode(current_command), self.encode(updated_command), self.ttl, int(terminal),
            self.encode(position) if position is not None else "",
            int(updated_command["status"] in {"QUEUED", "AWAITING_ACK", "SUCCEEDED"}),
            datetime.fromisoformat(current_command["deadline"]).timestamp(),
        ))

    def dispatch_key(self, command: RobotCommandRecord) -> str:
        return self.command_key(command["robot_id"], command["command_id"]) + f":dispatch:{command['attempt']}"

    async def dispatched(self, command: RobotCommandRecord) -> bool:
        return bool(await self.redis_client.get(self.dispatch_key(command)))

    async def mark_dispatched(self, command: RobotCommandRecord) -> None:
        await self.redis_client.set(self.dispatch_key(command), "1", expire=self.ttl)

    async def active_robot_ids(self) -> AsyncIterator[UUID]:
        async for active_command_key in self.redis_client.scan_iter("robot:*:active_command"):
            try:
                yield UUID(active_command_key.split(":")[1])
            except (ValueError, IndexError):
                continue
