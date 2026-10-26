from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from fastapi import FastAPI

from app.common.context import AppContext
from app.common.enum.context_actions import ASSIGN_DOCKING_STATION_ROBOT
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
)
from app.common.schemas.map import DockingStationRobotAssignmentDTO
from app.common.schemas.user import Credential
from app.core.rbac.permissions import PermissionService
from app.services.docking_station import DockingStationService
from app.services.map import MapService
from app.handler.map import MapHandler
from app.router.map import MapRouter


def setup(
    role=GroupRole.ADMIN,
    *,
    map_exists=True,
    station_exists=True,
    station_robot_id=None,
    robot_exists=True,
    robot_map_id=None,
    robot_already_assigned=False
):
    group_id, map_id, station_id, robot_id = (uuid4() for _ in range(4))
    credential = Credential(
        id=uuid4(),
        email="assignment@example.com",
        status=UserStatus.ACTIVE,
        active_group_id=group_id,
    )
    calls = []
    maps = SimpleNamespace(
        get_by_id_and_group_for_update=AsyncMock(
            side_effect=lambda **kwargs: calls.append("map")
            or (SimpleNamespace(id=map_id) if map_exists else None)
        )
    )
    station = SimpleNamespace(robot_id=station_robot_id)
    stations = SimpleNamespace(
        get_by_id_and_map_for_update=AsyncMock(
            side_effect=lambda **kwargs: calls.append("station")
            or (station if station_exists else None)
        ),
        is_robot_assigned=AsyncMock(return_value=robot_already_assigned),
        assign_robot=AsyncMock(),
    )
    bots = SimpleNamespace(
        get_by_id_and_group_for_update=AsyncMock(
            side_effect=lambda **kwargs: calls.append("robot")
            or (
                SimpleNamespace(map_id=robot_map_id or map_id) if robot_exists else None
            )
        )
    )

    async def transaction(callback):
        return await callback(object())

    registry = SimpleNamespace(
        map_repo=lambda: maps,
        docking_station_repo=lambda: stations,
        bot_repo=lambda: bots,
        transaction_wrapper=AsyncMock(side_effect=transaction),
    )
    permissions = SimpleNamespace(
        get_group_member=AsyncMock(
            return_value=SimpleNamespace(role=role) if role else None
        ),
        is_action_executable=PermissionService.is_action_executable,
    )
    docking = DockingStationService(registry, permissions)
    service = MapService(registry, permissions, docking_station_service=docking)
    ctx = AppContext(
        trace_id=uuid4(), actor=credential.id, action=ASSIGN_DOCKING_STATION_ROBOT
    )
    return (
        service,
        credential,
        ctx,
        map_id,
        station_id,
        robot_id,
        maps,
        stations,
        bots,
        calls,
    )


def assign(service, credential, ctx, map_id, station_id, robot_id):
    return service.assign_docking_station_robot(
        map_id=map_id,
        docking_id=station_id,
        robot_id=robot_id,
        group_id=credential.active_group_id,
        credential=credential,
        ctx=ctx,
    )


def test_assignment_request_requires_one_uuid_and_rejects_unknown_fields():
    robot_id = uuid4()
    assert DockingStationRobotAssignmentDTO(robot_id=robot_id).robot_id == robot_id
    for payload in (
        {},
        {"robot_id": None},
        {"robot_id": str(robot_id), "map_id": str(uuid4())},
    ):
        with pytest.raises(ValidationError):
            DockingStationRobotAssignmentDTO.model_validate(payload)


def test_assignment_route_and_openapi_contract():
    app = FastAPI()
    app.include_router(
        MapRouter(MapHandler(SimpleNamespace())).router, prefix="/api/v1/maps"
    )
    operation = app.openapi()["paths"]["/api/v1/maps/{map_id}/docking/{docking_id}/"][
        "put"
    ]
    assert operation["responses"]["204"]["description"] == "Successful Response"
    assert set(operation["responses"]) >= {
        "204",
        "400",
        "401",
        "403",
        "404",
        "409",
        "422",
    }
    schema_ref = operation["requestBody"]["content"]["application/json"]["schema"][
        "$ref"
    ]
    assignment_schema = app.openapi()["components"]["schemas"][
        schema_ref.rsplit("/", 1)[1]
    ]
    assert assignment_schema["required"] == ["robot_id"]
    assert assignment_schema["additionalProperties"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [GroupRole.OWNER, GroupRole.ADMIN])
async def test_assignment_locks_map_station_robot_then_updates_only_assignment(role):
    (
        service,
        credential,
        ctx,
        map_id,
        station_id,
        robot_id,
        maps,
        stations,
        bots,
        calls,
    ) = setup(role)
    await assign(service, credential, ctx, map_id, station_id, robot_id)
    assert calls == ["map", "station", "robot"]
    maps.get_by_id_and_group_for_update.assert_awaited_once()
    stations.get_by_id_and_map_for_update.assert_awaited_once_with(
        session=maps.get_by_id_and_group_for_update.call_args.kwargs["session"],
        station_id=station_id,
        map_id=map_id,
    )
    stations.assign_robot.assert_awaited_once_with(
        session=maps.get_by_id_and_group_for_update.call_args.kwargs["session"],
        station=stations.assign_robot.call_args.kwargs["station"],
        robot_id=robot_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role", [GroupRole.GUEST, GroupRole.MEMBER, GroupRole.MODERATOR, None]
)
async def test_assignment_requires_owner_or_admin(role):
    service, credential, ctx, map_id, station_id, robot_id, maps, *_ = setup(role)
    with pytest.raises(ForbiddenException):
        await assign(service, credential, ctx, map_id, station_id, robot_id)
    maps.get_by_id_and_group_for_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_assignment_rejects_missing_map_or_scoped_station_or_robot():
    for options, error in [
        ({"map_exists": False}, NotFoundException),
        ({"station_exists": False}, NotFoundException),
        ({"robot_exists": False}, NotFoundException),
    ]:
        service, credential, ctx, map_id, station_id, robot_id, *_ = setup(**options)
        with pytest.raises(error):
            await assign(service, credential, ctx, map_id, station_id, robot_id)


@pytest.mark.asyncio
async def test_assignment_rejects_wrong_map_robot_as_bad_request():
    service, credential, ctx, map_id, station_id, robot_id, _, stations, *_ = setup(
        robot_map_id=uuid4()
    )
    with pytest.raises(BadRequestException) as error:
        await assign(service, credential, ctx, map_id, station_id, robot_id)
    assert error.value.status_code == 400
    stations.assign_robot.assert_not_awaited()


@pytest.mark.asyncio
async def test_assignment_rejects_occupied_station_and_duplicate_robot_with_conflict():
    service, credential, ctx, map_id, station_id, robot_id, _, stations, *_ = setup(
        station_robot_id=uuid4()
    )
    with pytest.raises(BadRequestException) as error:
        await assign(service, credential, ctx, map_id, station_id, robot_id)
    assert error.value.status_code == 409
    stations.is_robot_assigned.assert_not_awaited()

    service, credential, ctx, map_id, station_id, robot_id, _, stations, *_ = setup(
        robot_already_assigned=True
    )
    with pytest.raises(BadRequestException) as error:
        await assign(service, credential, ctx, map_id, station_id, robot_id)
    assert error.value.status_code == 409
    stations.assign_robot.assert_not_awaited()
