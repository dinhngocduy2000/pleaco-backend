"""Atomic cleaning-task operations against the disposable Redis instance."""

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import ConnectionPool

from app.external.redis.redis import RedisClient
from app.external.redis.robot_commands import RobotCommandStore

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get("PLEACO_ROBOT_ACTION_INTEGRATION") != "1",
    reason="Requires the disposable robot-action Redis service",
)]


@pytest_asyncio.fixture
async def store():
    client = RedisClient.__new__(RedisClient)
    client.pool = ConnectionPool(host="127.0.0.1", port=6380, db=8, decode_responses=True)
    try:
        yield RobotCommandStore(client)
    finally:
        await client.pool.disconnect()


def build_task_records():
    """Build a two-move route and the reservation for its first command."""
    now = datetime.now(timezone.utc)
    origin, target = {"x": 0, "y": 0}, {"x": 1, "y": 0}
    task = {
        "task_id": str(uuid4()), "robot_id": str(uuid4()), "group_id": str(uuid4()),
        "map_id": str(uuid4()), "status": "QUEUED", "completed_moves": 0,
        "total_moves": 2, "next_waypoint": target, "next_check_at": now.isoformat(),
        "occupancy_retries_used": 0, "terminal_reason": None, "layout_signature": "layout",
    }
    command = {
        **{key: task[key] for key in ("robot_id", "group_id", "map_id", "task_id")},
        "command_id": str(uuid4()), "attempt": 1, "status": "QUEUED",
        "action": {
            "type": "CLEANING", "direction": "EAST", "distance": "1m",
            "is_start": True, "is_finish": False,
        },
        "origin": origin, "target": target, "created_at": now.isoformat(),
        "updated_at": now.isoformat(), "not_before": now.isoformat(),
        "deadline": (now + timedelta(minutes=1)).isoformat(), "failure_reason": None,
    }
    moves = [
        {"origin": origin, "target": target, "direction": "EAST", "distance": "1m"},
        {"origin": target, "target": origin, "direction": "WEST", "distance": "1m"},
    ]
    return task, command, moves


async def test_task_transition_cas_commits_progress_and_successor(store):
    task, command, moves = build_task_records()
    robot_id = command["robot_id"]
    peer_id = str(uuid4())
    successor_id = str(uuid4())
    peer_state = await store.peer_state(peer_id)
    assert await store.reserve_task(command, None, task, moves)
    assert not await store.reserve(command, None)
    assert await store.get_task_move(task["task_id"], 1) == moves[1]

    position_key = store.position_key(peer_id)
    try:
        await store.redis_client.set(position_key, store.encode({"x": 2, "y": 2}), expire=60)
        awaiting = {**command, "status": "AWAITING_ACK"}
        awaiting_task = {**task, "status": "AWAITING_ACK"}
        assert not await store.transition_task(command, awaiting, task, awaiting_task, guard=peer_state.guard)

        current_peer_state = await store.peer_state(peer_id)
        assert await store.transition_task(command, awaiting, task, awaiting_task, guard=current_peer_state.guard)
        position = {
            **{key: command[key] for key in ("robot_id", "group_id", "map_id", "command_id", "updated_at")},
            **command["target"],
        }
        future = (datetime.now(timezone.utc) + timedelta(seconds=20)).isoformat()
        successor = {
            **command, "command_id": successor_id, "origin": moves[1]["origin"],
            "target": moves[1]["target"], "not_before": future,
        }
        waiting = {
            **task, "status": "WAITING_INTERVAL", "completed_moves": 1,
            "next_check_at": future, "next_waypoint": successor["target"],
        }
        succeeded = {**awaiting, "status": "SUCCEEDED"}
        assert await store.transition_task(
            awaiting, succeeded, awaiting_task, waiting, position=position, next_command=successor,
        )
        assert not await store.transition_task(
            awaiting, succeeded, awaiting_task, waiting, position=position, next_command=successor,
        )
        assert await store.active(robot_id) == successor
        assert await store.position(robot_id) == position
        assert await store.get_task(task["task_id"]) == waiting
        assert not await store.transition_task(
            successor, {**successor, "status": "AWAITING_ACK"}, waiting, awaiting_task,
        )
        failed = {**waiting, "status": "FAILED", "terminal_reason": "Robot disconnected"}
        assert await store.transition_task(
            successor, {**successor, "status": "CANCELLED"}, waiting, failed, terminal=True,
        )
        assert await store.active(robot_id) is None
        assert await store.get_task(task["task_id"]) == failed
        assert not await store.has_task_moves(task["task_id"])
        assert await store.position(robot_id) is None
        assert await store.reserve(command, None)
    finally:
        for key in (
            position_key, store.active_key(robot_id), store.active_task_key(robot_id),
            store.command_key(robot_id, command["command_id"]),
            store.command_key(robot_id, successor_id),
            store.task_key(task["task_id"]), store.task_moves_key(task["task_id"]),
            store.position_key(robot_id),
        ):
            await store.redis_client.delete(key)


async def test_evicted_active_command_fails_retained_task(store):
    task, command, moves = build_task_records()
    robot_id = command["robot_id"]
    assert await store.reserve_task(command, None, task, moves)
    try:
        await store.redis_client.set(store.position_key(robot_id), store.encode(command["origin"]), expire=60)
        await store.redis_client.delete(store.command_key(robot_id, command["command_id"]))
        assert await store.active(robot_id) is None
        assert await store.position(robot_id) is None
        assert (await store.get_task(task["task_id"]))["status"] == "FAILED"
        assert not await store.has_task_moves(task["task_id"])
        assert await store.reserve(command, None)
    finally:
        for key in (
            store.active_key(robot_id), store.active_task_key(robot_id),
            store.command_key(robot_id, command["command_id"]),
            store.task_key(task["task_id"]), store.task_moves_key(task["task_id"]),
        ):
            await store.redis_client.delete(key)


async def test_timeout_keeps_occupancy_until_matching_position_is_confirmed(store):
    task, command, moves = build_task_records()
    robot_id = command["robot_id"]
    command["origin_verified"] = True
    awaiting = {**command, "status": "AWAITING_ACK"}
    awaiting_task = {**task, "status": "AWAITING_ACK"}
    timed_out = {**command, "status": "TIMED_OUT"}
    timed_out_task = {**task, "status": "TIMED_OUT"}
    position = {
        **{key: command[key] for key in ("robot_id", "group_id", "map_id", "command_id", "updated_at")},
        **command["target"], "verified": True,
    }
    movement_key = store.movement_reservation_key(robot_id)
    active_key = store.active_key(robot_id)
    try:
        assert await store.reserve_task(command, None, task, moves)
        assert await store.transition_task(command, awaiting, task, awaiting_task)
        assert await store.transition_task(awaiting, timed_out, awaiting_task, timed_out_task, terminal=True)
        peer = await store.peer_state(robot_id)
        assert peer.command is None
        assert peer.unconfirmed_movement == awaiting
        assert movement_key in peer.guard
        assert not await store.reserve(command, None)
        assert not await store.confirm_unconfirmed_movement({**awaiting, "attempt": 2}, position)

        await store.redis_client.set(active_key, str(uuid4()), expire=60)
        assert not await store.confirm_unconfirmed_movement(awaiting, position)
        await store.redis_client.delete(active_key)
        assert await store.confirm_unconfirmed_movement(awaiting, position)
        assert not await store.confirm_unconfirmed_movement(awaiting, position)
        assert (await store.get_task(task["task_id"]))["status"] == "TIMED_OUT"
        assert await store.position(robot_id) == position
        assert (await store.peer_state(robot_id)).unconfirmed_movement is None
    finally:
        for key in (
            active_key,
            store.active_task_key(robot_id), store.command_key(robot_id, command["command_id"]),
            store.task_key(task["task_id"]), store.task_moves_key(task["task_id"]),
            store.position_key(robot_id), movement_key,
        ):
            await store.redis_client.delete(key)


@pytest.mark.parametrize("status", ["FAILED", "CANCELLED", "TIMED_OUT"])
@pytest.mark.parametrize("task_present", [True, False])
async def test_interrupted_task_atomically_clears_only_its_robot_position(store, status, task_present):
    task, command, moves = build_task_records()
    robot_id = command["robot_id"]
    position_key = store.position_key(robot_id)
    peer_key = store.position_key(uuid4())
    position = {**command["target"], "updated_at": command["updated_at"]}
    keys = [position_key, peer_key, store.active_key(robot_id), store.active_task_key(robot_id),
            store.command_key(robot_id, command["command_id"]), store.task_key(task["task_id"]),
            store.task_moves_key(task["task_id"]), store.movement_reservation_key(robot_id)]
    try:
        assert await store.reserve_task(command, None, task, moves)
        await store.redis_client.set(position_key, store.encode(position), expire=60)
        await store.redis_client.set(peer_key, store.encode(position), expire=60)
        updated = {**command, "status": status}
        if task_present:
            async def interrupt():
                return await store.transition_task(
                    command, updated, task, {**task, "status": status}, terminal=True, position=position,
                )
        else:
            await store.redis_client.delete(store.task_key(task["task_id"]))
            async def interrupt():
                return await store.transition(command, updated, terminal=True, position=position)
        assert await interrupt()
        assert await store.position(robot_id) is None
        assert await store.redis_client.get(peer_key) == store.encode(position)
        # A redelivered terminal transition cannot erase a newer saved position.
        await store.redis_client.set(position_key, store.encode(position), expire=60)
        assert not await interrupt()
        assert await store.position(robot_id) == position
    finally:
        for key in keys:
            await store.redis_client.delete(key)


async def test_expired_task_cannot_write_confirmed_position(store):
    task, command, moves = build_task_records()
    robot_id = command["robot_id"]
    command["status"] = "AWAITING_ACK"
    command["deadline"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    task["status"] = "AWAITING_ACK"
    position = {
        **{key: command[key] for key in ("robot_id", "group_id", "map_id", "command_id", "updated_at")},
        **command["target"],
    }
    try:
        assert await store.reserve_task(command, None, task, moves)
        failed_command = {**command, "status": "FAILED"}
        failed_task = {**task, "status": "FAILED"}
        assert not await store.transition_task(
            command, failed_command, task, failed_task, position=position, terminal=True,
        )
        await store.redis_client.delete(store.task_key(task["task_id"]))
        assert not await store.transition(command, failed_command, position=position, terminal=True)
        assert await store.transition(command, failed_command, terminal=True)
        assert await store.position(robot_id) is None
        assert await store.active(robot_id) is None
        assert not await store.has_task_moves(task["task_id"])
    finally:
        for key in (
            store.active_key(robot_id), store.active_task_key(robot_id),
            store.command_key(robot_id, command["command_id"]), store.position_key(robot_id),
            store.task_key(task["task_id"]), store.task_moves_key(task["task_id"]),
        ):
            await store.redis_client.delete(key)
