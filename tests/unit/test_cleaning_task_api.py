from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.common.enum.direction import Direction
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import BadRequestException
from app.common.middleware.auth_middleware import AuthMiddleware
from app.common.schemas.cleaning_task import CleaningTaskInfo, CleaningTaskStatus
from app.common.schemas.user import Credential
from app.core.rbac.permissions import PermissionService
from app.handler.cleaning_task import CleaningTaskHandler
from app.router.cleaning_task import CleaningTaskRouter
from app.services.cleaning_path import CleaningPathService, PlannedCleaningPath
from app.services.cleaning_task import CleaningTaskService, build_cleaning_moves


def _path() -> PlannedCleaningPath:
    return PlannedCleaningPath(
        sections={
            "ring_layer_1": [(1, 1), (0.5, 1), (0.5, 2)],
            "ring_layer_2": [(0.5, 2), (1.5, 2)],
            "to_center": [(1.5, 2), (2.5, 2), (2.5, 2.5)],
            "return_to_dock": [(2.5, 2.5), (2.5, 1), (1, 1)],
        },
        start=(1, 1), center=(2.5, 2.5), start_position=None, layout_signature="layout",
        confirmed_peer_ids=[],
    )


def test_moves_follow_ring_center_and_return_order():
    moves = build_cleaning_moves(_path())
    assert [move["direction"] for move in moves] == [
        Direction.WEST, Direction.NORTH, Direction.EAST, Direction.EAST,
        Direction.NORTH, Direction.SOUTH, Direction.WEST,
    ]
    assert moves[0]["distance"] == "0.5m"
    assert moves[0]["origin"] == moves[-1]["target"] == {"x": 1, "y": 1}
    assert all(a["target"] == b["origin"] for a, b in zip(moves, moves[1:]))


@pytest.mark.parametrize("section,points,message", [
    ("to_center", [(1.5, 2)], "must reach the map center"),
    ("return_to_dock", [(2.5, 2.5), (1.5, 1)], "return to the starting dock"),
    ("ring_layer_1", [(1, 1), (0.5, 2)], "non-cardinal move"),
    ("ring_layer_2", [(0.5, 1.5), (1.5, 2)], "sections are disconnected"),
])
def test_invalid_route_is_rejected_before_any_moves_are_queued(section, points, message):
    path = _path()
    path.sections[section] = points
    with pytest.raises(BadRequestException, match=message):
        build_cleaning_moves(path)


async def test_http_start_and_status_authorize_and_scope_tasks(caplog):
    map_id, robot_id, group_id, task_id = uuid4(), uuid4(), uuid4(), uuid4()
    task = CleaningTaskInfo(
        task_id=task_id, robot_id=robot_id, group_id=group_id, map_id=map_id,
        status=CleaningTaskStatus.QUEUED, completed_moves=0, total_moves=7,
        occupancy_retries_used=0,
    )
    role = SimpleNamespace(role=GroupRole.MEMBER)
    permission = SimpleNamespace(
        get_group_member=AsyncMock(return_value=role),
        is_action_executable=PermissionService.is_action_executable,
    )
    planner = SimpleNamespace(plan_for_robot=AsyncMock(return_value=_path()))
    dispatcher = SimpleNamespace(
        ensure_cleaning_task_can_start=AsyncMock(),
        start_cleaning_task=AsyncMock(return_value=task),
        get_cleaning_task=AsyncMock(return_value=task),
    )
    credential = Credential(id=uuid4(), email="operator@example.com",
                            status=UserStatus.ACTIVE, active_group_id=group_id)
    app = FastAPI()
    service = CleaningTaskService(planner, dispatcher, permission)
    app.include_router(CleaningTaskRouter(CleaningTaskHandler(service)).router, prefix="/api/v1/maps")
    app.dependency_overrides[AuthMiddleware.auth_middleware] = lambda: credential
    url = f"/api/v1/maps/{map_id}/cleaning-tasks"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(url, json={"robot_id": str(robot_id)})
        assert response.status_code == 202, response.text
        assert response.json()["data"]["task_id"] == str(task_id)
        dispatcher.ensure_cleaning_task_can_start.assert_awaited_once_with(
            robot_id=robot_id, map_id=map_id, group_id=group_id, actor_id=credential.id,
            ctx=planner.plan_for_robot.call_args.kwargs["ctx"],
        )
        assert dispatcher.start_cleaning_task.call_args.kwargs["moves"] == build_cleaning_moves(_path())
        assert dispatcher.start_cleaning_task.call_args.kwargs["confirmed_peer_ids"] == []
        assert planner.plan_for_robot.call_args.kwargs["offline_status_code"] == 409
        request_context = planner.plan_for_robot.call_args.kwargs["ctx"]
        assert dispatcher.start_cleaning_task.call_args.kwargs["ctx"] is request_context
        assert any(
            str(request_context.trace_id) in record.message
            and "Compiled 7 cleaning movements" in record.message
            for record in caplog.records
        )

        dispatcher.ensure_cleaning_task_can_start.side_effect = BadRequestException(message="Robot is busy", status_code=409)
        assert (await client.post(url, json={"robot_id": str(robot_id)})).status_code == 409
        planner.plan_for_robot.assert_awaited_once()

        task_url = f"{url}/{task_id}"
        assert (await client.get(task_url)).status_code == 200

        role.role = GroupRole.GUEST
        assert (await client.post(url, json={"robot_id": str(robot_id)})).status_code == 403
        assert (await client.get(task_url)).status_code == 403

        role.role = GroupRole.MEMBER
        wrong_map = f"/api/v1/maps/{uuid4()}/cleaning-tasks/{task_id}"
        assert (await client.get(wrong_map)).status_code == 404
        credential.active_group_id = uuid4()
        assert (await client.get(task_url)).status_code == 404


async def test_planning_retains_the_source_record_for_the_start_reservation():
    map_id, robot_id, group_id, actor_id, peer_id, docked_peer_id = (uuid4() for _ in range(6))
    source_record = {
        "robot_id": str(robot_id), "map_id": str(map_id), "group_id": str(group_id),
        "x": 1.25, "y": 1.25, "command_id": str(uuid4()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    snapshot = {
        "width": 10, "height": 10, "boundary": "DIMENSIONS", "zones": [],
        "docks": [
            (robot_id, '{"type":"Polygon","coordinates":[[[0,0],[2,0],[2,2],[0,2],[0,0]]]}'),
            (docked_peer_id, '{"type":"Polygon","coordinates":[[[9,0],[10,0],[10,1],[9,1],[9,0]]]}'),
        ],
        "robots": [
            SimpleNamespace(id=current_id, map_id=map_id, group_id=group_id, connection_status="ONLINE")
            for current_id in (robot_id, peer_id, docked_peer_id)
        ],
    }
    peer_record = {**source_record, "robot_id": str(peer_id), "x": 9, "y": 9}
    position_records = {robot_id: source_record, peer_id: peer_record}
    store = SimpleNamespace(
        position=AsyncMock(side_effect=position_records.get),
        redis_client=SimpleNamespace(get=AsyncMock(return_value=source_record["updated_at"])),
    )
    service = CleaningPathService(
        SimpleNamespace(transaction_wrapper=AsyncMock(return_value=snapshot)), None, store, None,
    )
    path = await service.plan_for_robot(
        map_id=map_id, robot_id=robot_id, group_id=group_id, actor_id=actor_id, ctx=None,
    )
    assert [call.args[0] for call in store.position.await_args_list] == [robot_id, peer_id, docked_peer_id]
    assert path.start_position is source_record
    assert path.confirmed_peer_ids == [str(peer_id)]
    moves = build_cleaning_moves(path)
    assert {"x": 5, "y": 5} in [move["target"] for move in moves]
    assert moves[0]["origin"] == moves[-1]["target"] == {"x": 1.25, "y": 1.25}
