from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.common.context import AppContext
from app.common.enum.context_actions import LIST_BOTS, SEND_ROBOT_COMMAND
from app.common.enum.docking_station import DockingStationHeading
from app.common.enum.direction import Direction
from app.common.enum.robot import RobotConnectionStatus
from app.common.enum.user_roles import GroupRole
from app.common.exceptions import BadRequestException, ForbiddenException, NotFoundException
from app.common.middleware.auth_middleware import AuthMiddleware
from app.common.schemas.robot_action import (
    CleaningAction, RobotAcknowledgement, RobotCommand, RobotCommandInfo,
    RobotCommandRecord, RobotCommandRequest, RobotPositionRecord,
    RobotLoopRequest,
)
from app.core.config import settings
from app.core.rbac.permissions import PermissionService
from app.handler.robot_action import RobotActionHandler
from app.router.robot_action import RobotActionRouter
from app.services.robot_action import RobotActionService


T = TypeVar("T")


class MemoryStore:
    """Service-level CAS fake; real Lua is covered by integration tests."""
    def __init__(self) -> None:
        self.records = {}
        self.active_ids = {}
        self.positions = {}
        self.dispatches = set()
        self.redis_client = SimpleNamespace(get=AsyncMock())

    async def get(self, robot_id: UUID | str, command_id: UUID | str | None) -> RobotCommandRecord | None:
        return deepcopy(self.records.get(str(command_id)))

    async def active(self, robot_id: UUID | str) -> RobotCommandRecord | None:
        return await self.get(robot_id, self.active_ids.get(str(robot_id)))

    async def position(self, robot_id: UUID | str) -> RobotPositionRecord | None:
        return deepcopy(self.positions.get(str(robot_id)))

    async def dispatched(self, record: RobotCommandRecord) -> bool:
        return (record["command_id"], record["attempt"]) in self.dispatches

    async def mark_dispatched(self, record: RobotCommandRecord) -> bool:
        if await self.active(record["robot_id"]) != record:
            return False
        self.dispatches.add((record["command_id"], record["attempt"]))
        return True

    async def reserve(self, record: RobotCommandRecord, position: RobotPositionRecord | None) -> bool:
        robot_id = record["robot_id"]
        if robot_id in self.active_ids or self.positions.get(robot_id) != position:
            return False
        self.active_ids[robot_id] = record["command_id"]
        self.records[record["command_id"]] = deepcopy(record)
        return True

    async def transition(self, previous: RobotCommandRecord, updated: RobotCommandRecord, *, terminal: bool = False, position: RobotPositionRecord | None = None) -> bool:
        robot_id, command_id = previous["robot_id"], previous["command_id"]
        if self.active_ids.get(robot_id) != command_id or self.records.get(command_id) != previous:
            return False
        self.records[command_id] = deepcopy(updated)
        if terminal:
            self.active_ids.pop(robot_id)
        if position is not None:
            self.positions[robot_id] = deepcopy(position)
        return True


def setup(role: GroupRole | None = GroupRole.MEMBER) -> SimpleNamespace:
    now = [datetime.now(timezone.utc)]
    robot_id, group_id, map_id = uuid4(), uuid4(), uuid4()
    context = dict(robot_id=robot_id, group_id=group_id, map_id=map_id,
                   connection_status=RobotConnectionStatus.ONLINE, operational_status="IDLE",
                   dimension_x=Decimal("10"), dimension_y=Decimal("10"), dock_x=4.0, dock_y=4.0)
    robot_action_repository = SimpleNamespace(get_context=AsyncMock(return_value=context), list_robot_ids=AsyncMock(return_value=[robot_id]))
    async def transaction(callback: Callable[[object], Awaitable[T]]) -> T:
        return await callback(object())
    registry = SimpleNamespace(transaction_wrapper=transaction, robot_action_repo=lambda: robot_action_repository)
    permission_service = SimpleNamespace(get_group_member=AsyncMock(return_value=SimpleNamespace(role=role)),
                                 is_action_executable=PermissionService.is_action_executable)
    robot_command_store = MemoryStore()
    robot_command_store.redis_client.get.return_value = now[0].isoformat()
    robot_action_topic = SimpleNamespace(set_handlers=lambda command_handler, ack_handler: None, ready=True,
                            start=AsyncMock(), publish_command=AsyncMock())
    robot_action_mqtt_gateway = SimpleNamespace(ready=True, publish_command=AsyncMock())
    robot_action_service = RobotActionService(registry, permission_service, robot_command_store, robot_action_topic, robot_action_mqtt_gateway, clock=lambda: now[0])
    credential = SimpleNamespace(id=uuid4(), active_group_id=group_id)
    kwargs = dict(robot_id=robot_id, credential=credential, group_id=group_id,
                  ctx=AppContext(trace_id=uuid4(), actor=credential.id, action=SEND_ROBOT_COMMAND))
    return SimpleNamespace(robot_action_service=robot_action_service, robot_command_store=robot_command_store, robot_action_topic=robot_action_topic, robot_action_mqtt_gateway=robot_action_mqtt_gateway, kwargs=kwargs,
                           context=context, now=now, robot_action_repository=robot_action_repository)


def request(**changes: object) -> RobotCommandRequest:
    return RobotCommandRequest(action=CleaningAction(is_start=True, is_finish=False, direction="NORTH", **changes))


async def submit(s: SimpleNamespace, payload: RobotCommandRequest | None = None) -> RobotCommandInfo:
    return await s.robot_action_service.submit(request=payload or request(), **s.kwargs)


def envelope(info: RobotCommandInfo) -> RobotCommand:
    return RobotCommand.model_validate({key: getattr(info, key) for key in RobotCommand.model_fields})


def ack(command: RobotCommand, success: bool = True, **changes: object) -> RobotAcknowledgement:
    return RobotAcknowledgement.model_validate({**command.model_dump(mode="json"), "ack": success, **changes})


@pytest.mark.asyncio
async def test_four_action_loop_commits_each_step_and_finishes_only_on_fourth() -> None:
    s = setup()
    info = await s.robot_action_service.submit_loop(
        request=RobotLoopRequest(direction="NORTH"), **s.kwargs,
    )
    assert info.attempt == 1
    assert info.action.is_start and not info.action.is_finish
    for attempt in range(1, 5):
        record = await s.robot_command_store.get(info.robot_id, info.command_id)
        command = RobotCommand.model_validate({key: record[key] for key in RobotCommand.model_fields})
        assert command.attempt == attempt and command.command_id == info.command_id
        assert (command.action.is_start, command.action.is_finish) == (attempt == 1, attempt == 4)
        await s.robot_action_service.process_command(command)
        await asyncio.gather(
            s.robot_action_service.process_message(ack(command)),
            s.robot_action_service.process_message(ack(command)),
        )
        position = await s.robot_command_store.position(info.robot_id)
        assert position["y"] == 4 + attempt
        updated = await s.robot_command_store.get(info.robot_id, info.command_id)
        assert updated["status"] == ("SUCCEEDED" if attempt == 4 else "QUEUED")
    assert await s.robot_command_store.active(info.robot_id) is None
    assert s.robot_action_topic.publish_command.await_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_step", [1, 3])
async def test_loop_nack_stops_without_retry(failed_step: int) -> None:
    s = setup()
    info = await s.robot_action_service.submit_loop(
        request=RobotLoopRequest(direction="NORTH"), **s.kwargs,
    )
    for step in range(1, failed_step + 1):
        record = await s.robot_command_store.get(info.robot_id, info.command_id)
        command = RobotCommand.model_validate({key: record[key] for key in RobotCommand.model_fields})
        await s.robot_action_service.process_command(command)
        await s.robot_action_service.process_message(ack(command, step != failed_step))
    record = await s.robot_command_store.get(info.robot_id, info.command_id)
    assert record["status"] == "FAILED"
    assert s.robot_action_topic.publish_command.await_count == failed_step
    position = await s.robot_command_store.position(info.robot_id)
    assert position is None if failed_step == 1 else position["y"] == 4 + failed_step - 1


@pytest.mark.asyncio
async def test_loop_no_direction_fits_fails_without_publication() -> None:
    s = setup()
    s.context["dimension_x"] = Decimal("2")
    s.context["dimension_y"] = Decimal("2")
    s.context["dock_x"] = s.context["dock_y"] = 1.0
    info = await s.robot_action_service.submit_loop(
        request=RobotLoopRequest(direction="NORTH", distance="3m"), **s.kwargs,
    )
    assert info.status.value == "FAILED"
    s.robot_action_topic.publish_command.assert_not_awaited()
    assert await s.robot_command_store.position(info.robot_id) is None


@pytest.mark.asyncio
async def test_loop_no_next_step_retains_last_confirmed_position() -> None:
    s = setup()
    s.context["dimension_x"] = Decimal("1")
    s.context["dimension_y"] = Decimal("1")
    s.context["dock_x"] = s.context["dock_y"] = 0.0
    info = await s.robot_action_service.submit_loop(
        request=RobotLoopRequest(direction="EAST", distance="1m"), **s.kwargs,
    )
    first = envelope(info)
    await s.robot_action_service.process_command(first)
    await s.robot_action_service.process_message(ack(first))
    # Shrink the assigned rectangle after the first step so no full move fits.
    s.context["dimension_x"] = Decimal("0")
    s.context["dimension_y"] = Decimal("0")
    second_record = await s.robot_command_store.get(info.robot_id, info.command_id)
    second = RobotCommand.model_validate({key: second_record[key] for key in RobotCommand.model_fields})
    await s.robot_action_service.process_command(second)
    await s.robot_action_service.process_message(ack(second))
    record = await s.robot_command_store.get(info.robot_id, info.command_id)
    assert record["status"] == "FAILED"
    assert (await s.robot_command_store.position(info.robot_id))["x"] == 0


@pytest.mark.asyncio
async def test_loop_stale_ack_and_timeout_preserve_last_confirmed_step() -> None:
    s = setup()
    info = await s.robot_action_service.submit_loop(
        request=RobotLoopRequest(direction="NORTH"), **s.kwargs,
    )
    first = envelope(info)
    await s.robot_action_service.process_command(first)
    await s.robot_action_service.process_message(ack(first))
    second_record = await s.robot_command_store.get(info.robot_id, info.command_id)
    second = RobotCommand.model_validate({key: second_record[key] for key in RobotCommand.model_fields})
    await s.robot_action_service.process_message(ack(first))
    assert (await s.robot_command_store.get(info.robot_id, info.command_id))["attempt"] == 2
    await s.robot_action_service.process_command(second)
    s.now[0] += timedelta(seconds=61)
    await s.robot_action_service.reconcile_robot(info.robot_id)
    await s.robot_action_service.process_message(ack(second))
    assert (await s.robot_command_store.get(info.robot_id, info.command_id))["status"] == "TIMED_OUT"
    assert (await s.robot_command_store.position(info.robot_id))["y"] == 5


@pytest.mark.asyncio
async def test_loop_recovers_queued_next_step_without_advancing_attempt() -> None:
    s = setup()
    info = await s.robot_action_service.submit_loop(
        request=RobotLoopRequest(direction="NORTH"), **s.kwargs,
    )
    first = envelope(info)
    await s.robot_action_service.process_command(first)
    s.robot_action_topic.publish_command.side_effect = RuntimeError("broker unavailable")
    await s.robot_action_service.process_message(ack(first))
    record = await s.robot_command_store.get(info.robot_id, info.command_id)
    assert record["attempt"] == 2 and record["status"] == "QUEUED"
    s.robot_action_topic.publish_command.side_effect = None
    await s.robot_action_service.reconcile_robot(info.robot_id)
    recovered = s.robot_action_topic.publish_command.await_args.args[0]
    assert recovered.attempt == 2 and recovered.command_id == info.command_id


@pytest.mark.parametrize("point,heading,expected", [
    ((0, 10), "NORTH", "SOUTH"), ((10, 10), "EAST", "SOUTH"),
    ((0, 0), "SOUTH", "NORTH"), ((10, 0), "WEST", "NORTH"),
    ((0, 5), "WEST", "NORTH"), ((10, 5), "EAST", "NORTH"),
    ((5, 9.5), "NORTH", "SOUTH"),
])
def test_loop_movement_edge_preferences(point: tuple[int, int], heading: str, expected: str) -> None:
    from app.common.utils.robot_movement import RectangleBoundary, select_movement
    x, y = map(Decimal, point)
    result = select_movement(RectangleBoundary(Decimal(10), Decimal(10)), x, y, Decimal(1), Direction(heading), width=Decimal(10), height=Decimal(10))
    assert result is not None and result[0].value == expected


@pytest.mark.parametrize("distance", ["0m", "-1m", "1km", "nanm", "1e3m", ".5m", "01m", "1 m", "1" * 65 + "m", 1])
def test_distance_validation(distance: str | int) -> None:
    with pytest.raises(ValidationError):
        request(distance=distance)


def test_contract_defaults_alias_and_strict_fields() -> None:
    assert DockingStationHeading is Direction
    assert request().action.distance == "1m"
    assert request(distance="0.5m").action.distance == "0.5m"
    for change in ({"is_start": 1}, {"is_finish": "false"}, {"type": "NAVIGATION"}, {"extra": True}):
        with pytest.raises(ValidationError):
            CleaningAction.model_validate({**request().action.model_dump(), **change})
    command = RobotCommand(robot_id=uuid4(), command_id=uuid4(), attempt=1, action=request().action)
    for change in ({"attempt": True}, {"attempt": "1"}, {"attempt": 5}, {"ack": "true"}):
        with pytest.raises(ValidationError):
            ack(command, **change)


@pytest.mark.asyncio
@pytest.mark.parametrize("direction,target", [("NORTH", (4, 4.5)), ("SOUTH", (4, 3.5)), ("EAST", (4.5, 4)), ("WEST", (3.5, 4))])
async def test_success_directions_and_duplicate_ack(direction: str, target: tuple[float, float]) -> None:
    s = setup()
    payload = request(distance="0.5m")
    payload.action.direction = Direction(direction)
    info = await submit(s, payload)
    command = envelope(info)
    assert await s.robot_command_store.position(command.robot_id) is None
    await s.robot_action_service.process_message(command)
    assert s.robot_action_mqtt_gateway.publish_command.await_count == 1
    await asyncio.gather(s.robot_action_service.process_message(ack(command)), s.robot_action_service.process_message(ack(command)))
    position = await s.robot_command_store.position(command.robot_id)
    assert (position["x"], position["y"]) == target
    assert (await s.robot_command_store.get(command.robot_id, command.command_id))["status"] == "SUCCEEDED"
    assert await s.robot_command_store.active(command.robot_id) is None


@pytest.mark.asyncio
async def test_existing_position_wins_and_busy_is_rejected() -> None:
    s = setup()
    s.robot_command_store.positions[str(s.kwargs["robot_id"])] = dict(group_id=str(s.context["group_id"]), map_id=str(s.context["map_id"]), x=2, y=2)
    info = await submit(s)
    with pytest.raises(BadRequestException) as error:
        await submit(s)
    assert error.value.status_code == 409
    await s.robot_action_service.process_message(envelope(info))
    await s.robot_action_service.process_message(ack(envelope(info)))
    assert (await s.robot_command_store.position(info.robot_id))["y"] == 3


@pytest.mark.asyncio
async def test_negative_ack_has_four_sends_and_no_position() -> None:
    s = setup()
    info = await submit(s)
    for attempt in range(1, 5):
        command = envelope(info).model_copy(update={"attempt": attempt})
        await s.robot_action_service.process_message(command)
        await s.robot_action_service.process_message(ack(command, False))
        # Old negative ACK must not create an extra attempt.
        await s.robot_action_service.process_message(ack(command, False))
    assert s.robot_action_mqtt_gateway.publish_command.await_count == 4
    assert s.robot_action_topic.publish_command.await_count == 4
    assert (await s.robot_command_store.get(info.robot_id, info.command_id))["status"] == "FAILED"
    assert await s.robot_command_store.position(info.robot_id) is None
    assert (await submit(s)).command_id != info.command_id


@pytest.mark.asyncio
async def test_new_entry_points_retry_and_movement_position() -> None:
    s = setup()
    info = await submit(s)
    for attempt in range(1, 5):
        command = envelope(info).model_copy(update={"attempt": attempt})
        await s.robot_action_service.process_command(command)
        await s.robot_action_service.process_ack(ack(command, False))
        await s.robot_action_service.process_ack(ack(command, False))
    assert s.robot_action_mqtt_gateway.publish_command.await_count == 4
    assert s.robot_action_topic.publish_command.await_count == 4
    assert (await s.robot_command_store.get(info.robot_id, info.command_id))["status"] == "FAILED"


@pytest.mark.asyncio
async def test_ack_without_retry_fails_first_attempt() -> None:
    s = setup()
    info = await submit(s)
    command = envelope(info)
    await s.robot_action_service.process_command(command)
    await s.robot_action_service.process_ack(ack(command, False), retry_enabled=False)
    record = await s.robot_command_store.get(info.robot_id, info.command_id)
    assert record["status"] == "FAILED"
    assert record["failure_reason"] == "Robot action failed; retries disabled"
    s.robot_action_topic.publish_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_success_callback_only_for_eligible_ack_and_optional_position() -> None:
    s = setup()
    info = await submit(s)
    command = envelope(info)
    await s.robot_action_service.process_command(command)
    callback = Mock(wraps=s.robot_action_service._confirmed_position)
    await s.robot_action_service.process_ack(ack(command, attempt=2), on_success_ack=callback)
    await s.robot_action_service.process_ack(ack(command, False), on_success_ack=callback)
    callback.assert_not_called()
    await s.robot_action_service.process_command(command.model_copy(update={"attempt": 2}))
    second = command.model_copy(update={"attempt": 2})
    await s.robot_action_service.process_ack(ack(second), on_success_ack=callback)
    callback.assert_called_once()
    position = await s.robot_command_store.position(info.robot_id)
    await s.robot_action_service.process_ack(ack(second), on_success_ack=callback)
    assert await s.robot_command_store.position(info.robot_id) == position
    callback.assert_called_once()

    next_info = await submit(s)
    next_command = envelope(next_info)
    await s.robot_action_service.process_command(next_command)
    await s.robot_action_service.process_ack(ack(next_command))
    assert (await s.robot_command_store.get(next_info.robot_id, next_info.command_id))["status"] == "SUCCEEDED"
    assert await s.robot_command_store.position(info.robot_id) == position


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["timeout", "offline", "stale", "map_changed"])
async def test_terminal_work_ignores_late_ack(condition: str) -> None:
    s = setup()
    info = await submit(s)
    command = envelope(info)
    await s.robot_action_service.process_message(command)
    if condition == "timeout":
        s.now[0] += timedelta(seconds=61)
    elif condition == "offline":
        s.context["connection_status"] = RobotConnectionStatus.OFFLINE
    elif condition == "stale":
        s.robot_command_store.redis_client.get.return_value = None
    else:
        s.context["map_id"] = uuid4()
    await s.robot_action_service.reconcile_robot(info.robot_id)
    await s.robot_action_service.process_message(ack(command))
    assert s.robot_action_mqtt_gateway.publish_command.await_count == 1
    assert await s.robot_command_store.position(info.robot_id) is None
    assert (await s.robot_command_store.get(info.robot_id, info.command_id))["status"] == ("TIMED_OUT" if condition == "timeout" else "CANCELLED")


@pytest.mark.asyncio
async def test_wrong_attempt_or_action_ack_cannot_move_position() -> None:
    s = setup()
    info = await submit(s)
    command = envelope(info)
    await s.robot_action_service.process_message(command)
    await s.robot_action_service.process_message(ack(command, attempt=2))
    altered = command.action.model_dump(mode="json")
    altered["distance"] = "2m"
    await s.robot_action_service.process_message(ack(command, action=altered))
    assert await s.robot_command_store.position(info.robot_id) is None


@pytest.mark.asyncio
async def test_ack_before_dispatch_marker_does_not_restore_active_command() -> None:
    s = setup()
    info = await submit(s)
    command = envelope(info)

    async def acknowledge_during_publish(*_args: object, **_kwargs: object) -> None:
        await s.robot_action_service.process_message(ack(command))

    s.robot_action_mqtt_gateway.publish_command.side_effect = acknowledge_during_publish
    await s.robot_action_service.process_message(command)
    assert (await s.robot_command_store.get(info.robot_id, info.command_id))["status"] == "SUCCEEDED"
    assert await s.robot_command_store.active(info.robot_id) is None
    assert await s.robot_command_store.position(info.robot_id) is not None
    assert (str(info.command_id), 1) not in s.robot_command_store.dispatches


@pytest.mark.asyncio
async def test_command_admission_requires_shared_consumers_and_respects_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    s = setup()
    s.robot_action_topic.ready = False
    with pytest.raises(BadRequestException) as unavailable:
        await submit(s)
    assert unavailable.value.status_code == 503
    s.robot_action_topic.ready = True
    s.robot_action_mqtt_gateway.ready = False
    with pytest.raises(BadRequestException) as mqtt_unavailable:
        await submit(s)
    assert mqtt_unavailable.value.status_code == 503
    s.robot_action_mqtt_gateway.ready = True
    monkeypatch.setattr(settings, "ROBOT_COMMANDS_PAUSED", True)
    with pytest.raises(BadRequestException) as paused:
        await submit(s)
    assert paused.value.status_code == 503
    assert not s.robot_command_store.records


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [GroupRole.GUEST, None])
async def test_guest_cannot_submit(role: GroupRole | None) -> None:
    s = setup(role)
    if role is None:
        s.robot_action_service.permission_service.get_group_member.return_value = None
    with pytest.raises(ForbiddenException):
        await submit(s)


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["other_group", "offline", "stale", "no_map", "no_origin", "out_of_bounds"])
async def test_rejected_commands_have_no_side_effects(condition: str) -> None:
    s = setup()
    if condition == "other_group":
        s.context["group_id"] = uuid4()
    elif condition == "offline":
        s.context["connection_status"] = RobotConnectionStatus.OFFLINE
    elif condition == "stale":
        s.robot_command_store.redis_client.get.return_value = None
    elif condition == "no_map":
        s.context["map_id"] = None
    elif condition == "no_origin":
        s.context["dock_x"] = None
    else:
        s.context["dock_y"] = 10
    with pytest.raises((BadRequestException, NotFoundException)):
        await submit(s)
    assert not s.robot_command_store.records
    s.robot_action_mqtt_gateway.publish_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_persisted_publication_intent_recovers_without_incrementing_attempt() -> None:
    s = setup()
    s.robot_action_topic.publish_command.side_effect = ConnectionError()
    info = await submit(s)
    assert (await s.robot_command_store.active(info.robot_id))["status"] == "QUEUED"
    s.robot_action_topic.publish_command.side_effect = None
    await s.robot_action_service.reconcile_robot(info.robot_id)
    assert s.robot_action_topic.publish_command.call_args.args[0].attempt == 1
    s.robot_action_mqtt_gateway.publish_command.side_effect = ConnectionError()
    with pytest.raises(ConnectionError):
        await s.robot_action_service.process_message(envelope(info))
    assert (await s.robot_command_store.active(info.robot_id))["status"] == "QUEUED"


def test_http_routes_validation_and_authentication() -> None:
    s = setup()
    app = FastAPI()
    app.include_router(RobotActionRouter(RobotActionHandler(s.robot_action_service)).router, prefix="/api/v1/bots")
    with TestClient(app) as client:
        assert client.post(f"/api/v1/bots/{s.kwargs['robot_id']}/commands", json={"action": request().action.model_dump(mode="json")}).status_code == 401
        app.dependency_overrides[AuthMiddleware.auth_middleware] = lambda: s.kwargs["credential"]
        assert client.post(f"/api/v1/bots/{s.kwargs['robot_id']}/commands", json={"action": {"direction": "UP"}}).status_code == 422
        result = client.post(f"/api/v1/bots/{s.kwargs['robot_id']}/commands", json={"action": request().action.model_dump(mode="json")})
        assert result.status_code == 202
        command_id = result.json()["data"]["command_id"]
        assert client.get(f"/api/v1/bots/{s.kwargs['robot_id']}/commands/{command_id}").status_code == 200
        assert client.get(f"/api/v1/bots/{s.kwargs['robot_id']}/position").json()["data"] is None


def test_loop_http_auth_validation_and_busy_robot() -> None:
    s = setup()
    app = FastAPI()
    app.include_router(RobotActionRouter(RobotActionHandler(s.robot_action_service)).router, prefix="/api/v1/bots")
    path = f"/api/v1/bots/{s.kwargs['robot_id']}/commands/loop"
    with TestClient(app) as client:
        assert client.post(path, json={"direction": "NORTH"}).status_code == 401
        app.dependency_overrides[AuthMiddleware.auth_middleware] = lambda: s.kwargs["credential"]
        for payload in ({"direction": "UP"}, {"direction": "NORTH", "distance": "0m"}, {"direction": "NORTH", "is_finish": True}):
            assert client.post(path, json=payload).status_code == 422
        result = client.post(path, json={"direction": "NORTH"})
        assert result.status_code == 202
        assert result.json()["data"]["attempt"] == 1
        assert client.post(path, json={"direction": "NORTH"}).status_code == 409
