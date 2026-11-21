"""Atomic command reservations, transitions, and confirmed position writes."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from app.common.schemas.cleaning_task import CleaningMove, CleaningTaskRecord
from app.common.schemas.robot_action import RobotCommandRecord, RobotPositionRecord
from app.external.redis.redis import RedisClient


@dataclass(frozen=True)
class RobotPeerState:
    """Peer records and the exact Redis values used to make an occupancy decision."""

    position: RobotPositionRecord | None
    command: RobotCommandRecord | None
    guard: dict[str, str | None]
    position_invalid: bool = False
    command_invalid: bool = False
    cleaning_task_active: bool = False


_CHECK_PEER_STATE = """
-- A missing key is encoded as an empty string. Prefix present values so an
-- empty Redis value remains distinct from an absent key.
local function peer_state_matches(first_key, first_arg)
    for index = first_key, #KEYS do
        local expected = ARGV[first_arg + index - first_key]
        local value = redis.call('GET', KEYS[index])
        local actual = value and 'v' .. value or ''
        if actual ~= expected then return false end
    end
    return true
end
"""


class RobotCommandStore:
    """Persist reservations, execution progress, and confirmed positions in Redis."""

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
    def task_key(task_id: UUID | str) -> str:
        return f"cleaning_task:{task_id}"

    @staticmethod
    def active_task_key(robot_id: UUID | str) -> str:
        return f"robot:{robot_id}:active_cleaning_task"

    @classmethod
    def task_moves_key(cls, task_id: UUID | str) -> str:
        return f"{cls.task_key(task_id)}:moves"

    @staticmethod
    def encode(value: object) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    async def get(self, robot_id: UUID | str, command_id: UUID | str) -> RobotCommandRecord | None:
        saved_command = await self.redis_client.get(self.command_key(robot_id, command_id))
        return cast(RobotCommandRecord, json.loads(saved_command)) if saved_command else None

    async def active(self, robot_id: UUID | str) -> RobotCommandRecord | None:
        command_id = await self.redis_client.get(self.active_key(robot_id))
        active_command = await self.get(robot_id, command_id) if command_id else None
        if active_command is None:
            await self._release_dangling_reservation(robot_id, command_id)
        return active_command

    async def _release_dangling_reservation(self, robot_id: UUID | str, command_id: str | None) -> None:
        """Fail retained tasks whose active commands were evicted."""
        task_index = self.active_task_key(robot_id)
        task_id = await self.redis_client.get(task_index)
        if not (command_id or task_id):
            return
        await self.redis_client.eval("""
            if (redis.call('GET', KEYS[1]) or '') ~= ARGV[1] then return 0 end
            if ARGV[1] ~= '' and redis.call('EXISTS', KEYS[2]) == 1 then return 0 end
            if (redis.call('GET', KEYS[3]) or '') ~= ARGV[2] then
                return 0
            end
            redis.call('DEL', KEYS[1], KEYS[3])
            if ARGV[2] ~= '' then
                local record = redis.call('GET', KEYS[4])
                if record then
                    local valid, task = pcall(cjson.decode, record)
                    if valid and type(task) == 'table' and task.task_id == ARGV[2] then
                        task.status = 'FAILED'
                        task.terminal_reason = 'Active command state is unavailable'
                        task.next_waypoint = cjson.null
                        task.next_check_at = cjson.null
                        redis.call('SET', KEYS[4], cjson.encode(task), 'EX', ARGV[3])
                    end
                end
                redis.call('DEL', KEYS[5])
            end
            return 1
        """, 5, self.active_key(robot_id), self.command_key(robot_id, command_id or ""),
            task_index, self.task_key(task_id or ""), self.task_moves_key(task_id or ""),
            command_id or "", task_id or "", self.ttl)

    async def position(self, robot_id: UUID | str) -> RobotPositionRecord | None:
        saved_position = await self.redis_client.get(self.position_key(robot_id))
        return cast(RobotPositionRecord, json.loads(saved_position)) if saved_position else None

    async def get_task(self, task_id: UUID | str) -> CleaningTaskRecord | None:
        """Read progress for an active or retained terminal task."""
        saved_task = await self.redis_client.get(self.task_key(task_id))
        return cast("CleaningTaskRecord", json.loads(saved_task)) if saved_task else None

    async def get_task_move(self, task_id: UUID | str, index: int) -> CleaningMove | None:
        """Read one move by its zero-based route index."""
        saved_move = await self.redis_client.eval(
            "return redis.call('HGET', KEYS[1], ARGV[1])", 1, self.task_moves_key(task_id), str(index),
        )
        return cast(CleaningMove, json.loads(saved_move)) if isinstance(saved_move, str) else None

    async def has_task_moves(self, task_id: UUID | str) -> bool:
        """Check for move zero, which exists in every stored nonempty route."""
        return await self.get_task_move(task_id, 0) is not None

    @staticmethod
    def _peer_record(value: str | None) -> dict[str, object] | None:
        if value is None:
            return None
        try:
            record = json.loads(value)
        except ValueError:
            return None
        return record if isinstance(record, dict) else None

    async def peer_state(self, robot_id: UUID | str) -> RobotPeerState:
        """Read a peer snapshot whose validity can be checked during dispatch."""
        active_key = self.active_key(robot_id)
        position_key = self.position_key(robot_id)
        task_index = self.active_task_key(robot_id)
        peer_keys = [active_key, position_key, task_index]
        peer_values = await self.redis_client.mget(peer_keys)
        command_id, saved_position, task_id = peer_values
        expected_peer_values = dict(zip(peer_keys, peer_values, strict=True))
        saved_command = None
        if command_id:
            key = self.command_key(robot_id, command_id)
            saved_command = await self.redis_client.get(key)
            expected_peer_values[key] = saved_command
        position = cast("RobotPositionRecord | None", self._peer_record(saved_position))
        command = cast("RobotCommandRecord | None", self._peer_record(saved_command))
        return RobotPeerState(
            position=position, command=command, guard=expected_peer_values,
            position_invalid=saved_position is not None and position is None,
            command_invalid=(command_id is not None or task_id is not None) and command is None,
            cleaning_task_active=task_id is not None or bool(command and command.get("task_id")),
        )

    async def reserve(self, command: RobotCommandRecord, position: RobotPositionRecord | None) -> bool:
        return await self._reserve(command, position)

    async def reserve_task(
        self, command: RobotCommandRecord, position: RobotPositionRecord | None,
        task: CleaningTaskRecord, moves: list[CleaningMove],
    ) -> bool:
        """Save the route and first command together if the robot is still free."""
        return await self._reserve(command, position, task=task, moves=moves)

    async def _reserve(
        self, command: RobotCommandRecord, position: RobotPositionRecord | None, *,
        task: CleaningTaskRecord | None = None, moves: list[CleaningMove] | None = None,
    ) -> bool:
        reserve_script = """
        if redis.call('EXISTS', KEYS[1], KEYS[4]) ~= 0 then return 0 end
        local position = redis.call('GET', KEYS[3]) or ''
        if position ~= ARGV[3] then return 0 end
        if #KEYS == 6 then
            if redis.call('EXISTS', KEYS[5], KEYS[6]) ~= 0 then return 0 end
            local moves = cjson.decode(ARGV[7])
            if #moves == 0 then return 0 end
            -- Keep each HSET call within Lua's argument limit while storing the whole route atomically.
            for first = 1, #moves, 500 do
                local entries = {}
                for index = first, math.min(first + 499, #moves) do
                    entries[#entries + 1] = tostring(index - 1)
                    entries[#entries + 1] = moves[index]
                end
                redis.call('HSET', KEYS[6], unpack(entries))
            end
            redis.call('EXPIRE', KEYS[6], ARGV[4])
            redis.call('SET', KEYS[4], ARGV[5], 'EX', ARGV[4])
            redis.call('SET', KEYS[5], ARGV[6], 'EX', ARGV[4])
        end
        redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[4])
        redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[4])
        return 1
        """
        keys = [
            self.active_key(command["robot_id"]),
            self.command_key(command["robot_id"], command["command_id"]),
            self.position_key(command["robot_id"]),
            self.active_task_key(command["robot_id"]),
        ]
        task_values = []
        if task is not None and moves is not None:
            task_id = command["task_id"]
            keys.extend([self.task_key(task_id), self.task_moves_key(task_id)])
            task_values.extend([task_id, self.encode(task), self.encode([self.encode(move) for move in moves])])
        return bool(await self.redis_client.eval(
            reserve_script, len(keys), *keys,
            command["command_id"], self.encode(command),
            self.encode(position) if position is not None else "", self.ttl, *task_values,
        ))

    async def transition(
        self, current_command: RobotCommandRecord, updated_command: RobotCommandRecord, *,
        terminal: bool = False, position: RobotPositionRecord | None = None,
        guard: dict[str, str | None] | None = None,
    ) -> bool:
        """Compare and replace the active command, optionally verifying peer state."""
        transition_script = _CHECK_PEER_STATE + """
        if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
        if redis.call('GET', KEYS[2]) ~= ARGV[2] then return 0 end
        if not peer_state_matches(6, 10) then return 0 end
        if ARGV[7] == '1' then
            local now = redis.call('TIME')
            if tonumber(now[1]) + tonumber(now[2]) / 1000000 >= tonumber(ARGV[8]) then return 0 end
        end
        redis.call('SET', KEYS[2], ARGV[3], 'EX', ARGV[4])
        if ARGV[5] == '1' then
            redis.call('DEL', KEYS[1])
            if ARGV[9] ~= '' and redis.call('GET', KEYS[4]) == ARGV[9] then
                redis.call('DEL', KEYS[4], KEYS[5])
            end
        else
            redis.call('EXPIRE', KEYS[1], ARGV[4])
        end
        if ARGV[6] ~= '' then
            redis.call('SET', KEYS[3], ARGV[6], 'EX', ARGV[4])
        end
        return 1
        """
        guard = guard or {}
        robot_id = current_command["robot_id"]
        task_id = current_command.get("task_id", "")
        checks_deadline = (
            updated_command["status"] in {"QUEUED", "AWAITING_ACK", "SUCCEEDED"}
            or (bool(task_id) and position is not None)
        )
        return bool(await self.redis_client.eval(
            transition_script, 5 + len(guard), self.active_key(robot_id),
            self.command_key(robot_id, current_command["command_id"]),
            self.position_key(robot_id), self.active_task_key(robot_id),
            self.task_moves_key(task_id), *guard, current_command["command_id"],
            self.encode(current_command), self.encode(updated_command), self.ttl, int(terminal),
            self.encode(position) if position is not None else "",
            int(checks_deadline),
            datetime.fromisoformat(current_command["deadline"]).timestamp(), task_id,
            *("v" + value if value is not None else "" for value in guard.values()),
        ))

    async def transition_task(
        self, current_command: RobotCommandRecord, updated_command: RobotCommandRecord,
        current_task: CleaningTaskRecord, updated_task: CleaningTaskRecord, *,
        terminal: bool = False, position: RobotPositionRecord | None = None,
        next_command: RobotCommandRecord | None = None, guard: dict[str, str | None] | None = None,
    ) -> bool:
        """Commit task and command progress together, retaining one active pointer."""
        transition_script = _CHECK_PEER_STATE + """
        if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
        if redis.call('GET', KEYS[2]) ~= ARGV[2] then return 0 end
        if redis.call('GET', KEYS[4]) ~= ARGV[7] then return 0 end
        -- Completion can clean up a task whose route hash was lost.
        if ARGV[5] == '0' and redis.call('EXISTS', KEYS[5]) == 0 then return 0 end
        if ARGV[9] ~= '' and redis.call('EXISTS', KEYS[6]) == 1 then return 0 end
        local indexed_task = redis.call('GET', KEYS[7])
        if indexed_task and indexed_task ~= ARGV[14] then return 0 end
        if not peer_state_matches(8, 15) then return 0 end

        local time = redis.call('TIME')
        -- Recheck the dispatch window at the same instant as the state change.
        local now = tonumber(time[1]) + tonumber(time[2]) / 1000000
        if ARGV[11] == '1' and now >= tonumber(ARGV[12]) then return 0 end
        if ARGV[13] ~= '' and now < tonumber(ARGV[13]) then return 0 end

        redis.call('SET', KEYS[2], ARGV[3], 'EX', ARGV[4])
        redis.call('SET', KEYS[4], ARGV[8], 'EX', ARGV[4])
        if ARGV[5] == '1' then
            redis.call('DEL', KEYS[1], KEYS[5], KEYS[7])
        else
            redis.call('EXPIRE', KEYS[5], ARGV[4])
            redis.call('SET', KEYS[7], ARGV[14], 'EX', ARGV[4])
            if ARGV[9] == '' then
                redis.call('EXPIRE', KEYS[1], ARGV[4])
            else
                -- Swap the active pointer only once the successor record exists.
                redis.call('SET', KEYS[6], ARGV[10], 'EX', ARGV[4])
                redis.call('SET', KEYS[1], ARGV[9], 'EX', ARGV[4])
            end
        end
        if ARGV[6] ~= '' then
            redis.call('SET', KEYS[3], ARGV[6], 'EX', ARGV[4])
        end
        return 1
        """
        checks_occupancy = guard is not None
        # An explicitly empty guard still means occupancy was checked on an empty map.
        guard = guard or {}
        robot_id = current_command["robot_id"]
        task_id = current_command["task_id"]
        command_key = self.command_key(robot_id, current_command["command_id"])
        next_command_id = next_command["command_id"] if next_command is not None else ""
        not_before = (
            current_command.get("not_before")
            if current_command["status"] == "QUEUED"
            and (updated_command["status"] == "AWAITING_ACK" or checks_occupancy)
            else None
        )
        checks_deadline = position is not None or updated_command["status"] in {"QUEUED", "AWAITING_ACK", "SUCCEEDED"}
        return bool(await self.redis_client.eval(
            transition_script, 7 + len(guard),
            self.active_key(robot_id), command_key, self.position_key(robot_id),
            self.task_key(task_id), self.task_moves_key(task_id),
            self.command_key(robot_id, next_command_id) if next_command is not None else command_key,
            self.active_task_key(robot_id), *guard,
            current_command["command_id"], self.encode(current_command), self.encode(updated_command),
            self.ttl, int(terminal), self.encode(position) if position is not None else "",
            self.encode(current_task), self.encode(updated_task), next_command_id,
            self.encode(next_command) if next_command is not None else "",
            int(checks_deadline),
            datetime.fromisoformat(current_command["deadline"]).timestamp(),
            datetime.fromisoformat(not_before).timestamp() if not_before else "", task_id,
            *("v" + value if value is not None else "" for value in guard.values()),
        ))

    def dispatch_key(self, command: RobotCommandRecord) -> str:
        return self.command_key(command["robot_id"], command["command_id"]) + f":dispatch:{command['attempt']}"

    async def dispatched(self, command: RobotCommandRecord) -> bool:
        return bool(await self.redis_client.get(self.dispatch_key(command)))

    async def mark_dispatched(self, command: RobotCommandRecord) -> bool:
        """Record publication only while this exact attempt remains active."""
        return bool(await self.redis_client.eval(
            """
            if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
            if redis.call('GET', KEYS[2]) ~= ARGV[2] then return 0 end
            return redis.call('SET', KEYS[3], '1', 'EX', ARGV[3]) and 1 or 0
            """, 3, self.active_key(command["robot_id"]),
            self.command_key(command["robot_id"], command["command_id"]),
            self.dispatch_key(command), command["command_id"],
            self.encode(command), self.ttl,
        ))

    async def active_robot_ids(self) -> AsyncIterator[UUID]:
        seen: set[UUID] = set()
        async for active_command_key in self.redis_client.scan_iter("robot:*:active_*"):
            try:
                _, robot_id_text, kind = active_command_key.split(":")
                robot_id = UUID(robot_id_text)
            except (ValueError, IndexError):
                continue
            if kind in {"active_command", "active_cleaning_task"} and robot_id not in seen:
                seen.add(robot_id)
                yield robot_id
