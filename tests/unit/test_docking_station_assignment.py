from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.common.context import AppContext
from app.common.enum.context_actions import SAVE_DOCKING_STATIONS
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import BaseException
from app.common.middleware.auth_middleware import AuthMiddleware
from app.common.schemas.user import Credential
from app.core.rbac.permissions import PermissionService
from app.handler.map import MapHandler
from app.repository.docking_station import DockingStationRobotConflict
from app.router.map import MapRouter
from app.services.docking_station import DockingStationService
from app.services.map import MapService


@pytest.fixture
def assignment():
    map_id, robot_id, docking_id, group_id = (uuid4() for _ in range(4))
    credential = Credential(
        id=uuid4(),
        email="test@example.com",
        status=UserStatus.ACTIVE,
        active_group_id=group_id,
    )
    maps = SimpleNamespace(
        get_by_id_and_group_for_update=AsyncMock(return_value=object())
    )
    bots = SimpleNamespace(
        get_by_id_and_group_for_update=AsyncMock(
            return_value=SimpleNamespace(map_id=map_id)
        )
    )
    docks = SimpleNamespace(
        get_for_map=AsyncMock(return_value=SimpleNamespace(robot_id=None)),
        robot_has_station=AsyncMock(return_value=False),
        assign_robot_if_empty=AsyncMock(return_value=True),
    )
    permissions = SimpleNamespace(
        get_group_member=AsyncMock(return_value=SimpleNamespace(role=GroupRole.ADMIN)),
        is_action_executable=PermissionService.is_action_executable,
    )

    async def transaction(callback):
        return await callback(SimpleNamespace())

    repo = SimpleNamespace(
        map_repo=lambda: maps,
        bot_repo=lambda: bots,
        docking_station_repo=lambda: docks,
        transaction_wrapper=transaction,
    )
    service = DockingStationService(repo, permissions)
    return SimpleNamespace(
        service=service,
        maps=maps,
        bots=bots,
        docks=docks,
        permissions=permissions,
        credential=credential,
        map_id=map_id,
        robot_id=robot_id,
        docking_id=docking_id,
        args=dict(
            map_id=map_id,
            docking_id=docking_id,
            robot_id=robot_id,
            group_id=group_id,
            credential=credential,
            ctx=AppContext(
                trace_id=uuid4(), action=SAVE_DOCKING_STATIONS, actor=credential.id
            ),
        ),
    )


@pytest.mark.parametrize("role", list(GroupRole))
async def test_assignment_roles(assignment, role):
    a = assignment
    a.permissions.get_group_member.return_value.role = role
    if role in (GroupRole.OWNER, GroupRole.ADMIN):
        await a.service.assign_robot(**a.args)
        a.docks.assign_robot_if_empty.assert_awaited_once()
        assert (
            a.maps.get_by_id_and_group_for_update.call_args.kwargs["group_id"]
            == a.credential.active_group_id
        )
        assert (
            a.bots.get_by_id_and_group_for_update.call_args.kwargs["group_id"]
            == a.credential.active_group_id
        )
        assert a.docks.get_for_map.call_args.kwargs["map_id"] == a.map_id
    else:
        with pytest.raises(BaseException) as caught:
            await a.service.assign_robot(**a.args)
        assert caught.value.status_code == 403
        a.maps.get_by_id_and_group_for_update.assert_not_awaited()


@pytest.mark.parametrize(
    "failure,status",
    [
        ("no_group", 403),
        ("wrong_group", 403),
        ("map_missing", 404),
        ("dock_missing", 404),
        ("robot_missing", 404),
        ("wrong_map", 400),
        ("unassigned_robot", 400),
        ("occupied", 409),
        ("same_robot", 409),
        ("robot_has_station", 409),
        ("race", 409),
        ("unique_conflict", 409),
    ],
)
async def test_assignment_failures(assignment, failure, status):
    a = assignment
    if failure == "no_group":
        a.args["group_id"] = None
    elif failure == "wrong_group":
        a.args["group_id"] = uuid4()
    elif failure == "map_missing":
        a.maps.get_by_id_and_group_for_update.return_value = None
    elif failure == "dock_missing":
        a.docks.get_for_map.return_value = None
    elif failure == "robot_missing":
        a.bots.get_by_id_and_group_for_update.return_value = None
    elif failure == "wrong_map":
        a.bots.get_by_id_and_group_for_update.return_value.map_id = uuid4()
    elif failure == "unassigned_robot":
        a.bots.get_by_id_and_group_for_update.return_value.map_id = None
    elif failure == "occupied":
        a.docks.get_for_map.return_value.robot_id = uuid4()
    elif failure == "same_robot":
        a.docks.get_for_map.return_value.robot_id = a.robot_id
    elif failure == "robot_has_station":
        a.docks.robot_has_station.return_value = True
    elif failure == "race":
        a.docks.assign_robot_if_empty.return_value = False
    elif failure == "unique_conflict":
        a.docks.assign_robot_if_empty.side_effect = DockingStationRobotConflict()
    with pytest.raises(BaseException) as caught:
        await a.service.assign_robot(**a.args)
    assert caught.value.status_code == status
    if failure not in ("race", "unique_conflict"):
        a.docks.assign_robot_if_empty.assert_not_awaited()


def make_app(a):
    app = FastAPI()
    service = MapService(
        a.service.repo, a.permissions, docking_station_service=a.service
    )
    app.include_router(MapRouter(MapHandler(service)).router, prefix="/api/v1/maps")
    app.dependency_overrides[AuthMiddleware.auth_middleware] = lambda: a.credential
    return app


async def test_endpoint_success_and_openapi(assignment):
    a = assignment
    app = make_app(a)
    path = f"/api/v1/maps/{a.map_id}/docking/{a.docking_id}/"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(path, json={"robot_id": str(a.robot_id)})
    assert response.status_code == 204
    assert response.content == b""
    schema = app.openapi()["components"]["schemas"]["DockingStationAssignRobotDTO"]
    assert schema["required"] == ["robot_id"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["robot_id"]["type"] == "string"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"robot_id": None},
        {"robot_id": "bad"},
        {"robot_id": str(uuid4()), "extra": True},
    ],
)
async def test_endpoint_invalid_body(assignment, payload):
    a = assignment
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(a)), base_url="http://test"
    ) as client:
        response = await client.put(
            f"/api/v1/maps/{a.map_id}/docking/{a.docking_id}/", json=payload
        )
    assert response.status_code == 422
    a.maps.get_by_id_and_group_for_update.assert_not_awaited()


async def test_endpoint_conflict_shape(assignment):
    a = assignment
    a.docks.get_for_map.return_value.robot_id = a.robot_id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(a)), base_url="http://test"
    ) as client:
        response = await client.put(
            f"/api/v1/maps/{a.map_id}/docking/{a.docking_id}/",
            json={"robot_id": str(a.robot_id)},
        )
    assert response.status_code == 409
    assert response.json() == {"detail": "Docking station already has a robot assigned"}


async def test_endpoint_requires_authentication(assignment):
    a = assignment
    app = make_app(a)
    app.dependency_overrides.clear()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            f"/api/v1/maps/{a.map_id}/docking/{a.docking_id}/",
            json={"robot_id": str(a.robot_id)},
        )
    assert response.status_code == 401
    a.maps.get_by_id_and_group_for_update.assert_not_awaited()
