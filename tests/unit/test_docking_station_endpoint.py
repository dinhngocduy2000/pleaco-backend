from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI

from app.common.context import AppContext
from app.common.enum.context_actions import (
    ASSIGN_DOCKING_STATION,
    SAVE_DOCKING_STATIONS,
)
from app.common.enum.docking_station import DockingStationHeading
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import BaseException
from app.common.schemas.map import (
    DockingStationRobotAssignmentDTO,
    DockingStationSaveItemDTO,
    DockingStationsSaveDTO,
)
from pydantic import ValidationError
from app.common.schemas.user import Credential
from app.core.rbac.permissions import PermissionService
from app.handler.map import MapHandler
from app.repository.docking_station import (
    BoundaryPlacementInspection,
    DockingStationRobotConflict,
)
from app.router.map import MapRouter
from app.services.docking_station import DockingStationService


def polygon():
    return {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 0]]]}


def placement_inspection(
    *,
    boundary_exists=True,
    geometry_valid=True,
    fully_covered=True,
    edge_aligned=True,
    protrusion_allowed=True,
):
    return BoundaryPlacementInspection(
        item_index=0,
        boundary_exists=boundary_exists,
        geometry_valid=geometry_valid,
        fully_covered=fully_covered,
        edge_aligned=edge_aligned,
        protrusion_allowed=protrusion_allowed,
    )


def setup_service(role=GroupRole.ADMIN):
    group_id, map_id, robot_id = uuid4(), uuid4(), uuid4()
    credential = Credential(
        id=uuid4(),
        email="station@example.com",
        status=UserStatus.ACTIVE,
        active_group_id=group_id,
    )
    maps = SimpleNamespace(
        get_by_id_and_group_for_update=AsyncMock(
            return_value=SimpleNamespace(id=map_id)
        )
    )
    bots = SimpleNamespace(
        get_by_ids_and_group_for_update=AsyncMock(
            return_value=[SimpleNamespace(id=robot_id, map_id=map_id)]
        )
    )
    now = datetime.now(timezone.utc)

    async def save_many(**kwargs):
        return [
            dict(
                id=station_id or uuid4(),
                map_id=kwargs["map_id"],
                robot_id=robot_id,
                heading=heading,
                geometry=polygon(),
                created_at=now,
                updated_at=now,
            )
            for station_id, robot_id, heading, geometry_json in kwargs["stations"]
        ]

    stations = SimpleNamespace(
        inspect_boundary_placement=AsyncMock(return_value=[placement_inspection()]),
        robots_with_other_map_stations=AsyncMock(return_value=set()),
        save_many=AsyncMock(side_effect=save_many),
        list_for_map=AsyncMock(return_value={}),
        delete_many=AsyncMock(),
        has_outside_boundary=AsyncMock(return_value=False),
        clear_assignments=AsyncMock(),
    )

    async def transaction(callback):
        return await callback(SimpleNamespace())

    permissions = SimpleNamespace(
        get_group_member=AsyncMock(
            return_value=SimpleNamespace(role=role) if role else None
        ),
        is_action_executable=PermissionService.is_action_executable,
    )
    registry = SimpleNamespace(
        map_repo=lambda: maps,
        bot_repo=lambda: bots,
        docking_station_repo=lambda: stations,
        transaction_wrapper=transaction,
    )
    return (
        DockingStationService(registry, permissions),
        credential,
        map_id,
        robot_id,
        maps,
        bots,
        stations,
    )


async def call(service, credential, map_id, **payload):
    return await service.save_docking_stations(
        station_save=DockingStationsSaveDTO(
            map_id=map_id,
            data=[DockingStationSaveItemDTO(geometry=polygon(), **payload)],
        ),
        group_id=credential.active_group_id,
        credential=credential,
        ctx=AppContext(
            trace_id=uuid4(), actor=credential.id, action=SAVE_DOCKING_STATIONS
        ),
    )


@pytest.mark.parametrize("heading", [None, *DockingStationHeading])
def test_heading_normalization(heading):
    dto = DockingStationSaveItemDTO(geometry=polygon(), heading=heading)
    assert dto.heading == (heading or DockingStationHeading.SOUTH)
    assert (
        DockingStationSaveItemDTO(geometry=polygon()).heading
        == DockingStationHeading.SOUTH
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", list(GroupRole) + [None])
async def test_roles(role):
    service, credential, map_id, _, _, _, stations = setup_service(role)
    if role in (GroupRole.OWNER, GroupRole.ADMIN):
        result = await call(service, credential, map_id)
        assert result[0].heading == DockingStationHeading.SOUTH
        assert result[0].robot_id is None
    else:
        with pytest.raises(BaseException) as error:
            await call(service, credential, map_id)
        assert error.value.status_code == 403
        stations.save_many.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,code",
    [
        ("no_group", 403),
        ("map", 404),
        ("robot", 404),
        ("robot_map", 400),
        ("boundary", 400),
        ("topology", 400),
        ("coverage", 400),
        ("edge", 400),
        ("duplicate", 409),
    ],
)
async def test_validation_prevents_insertion(failure, code):
    service, credential, map_id, robot_id, maps, bots, stations = setup_service()
    if failure == "no_group":
        credential.active_group_id = None
    if failure == "map":
        maps.get_by_id_and_group_for_update.return_value = None
    if failure == "robot":
        bots.get_by_ids_and_group_for_update.return_value = []
    if failure == "robot_map":
        bots.get_by_ids_and_group_for_update.return_value[0].map_id = uuid4()
    if failure == "boundary":
        stations.inspect_boundary_placement.return_value = [
            placement_inspection(
                boundary_exists=False,
                fully_covered=False,
                edge_aligned=False,
                protrusion_allowed=False,
            )
        ]
    if failure == "topology":
        stations.inspect_boundary_placement.return_value = [
            placement_inspection(
                geometry_valid=False,
                fully_covered=False,
                edge_aligned=False,
                protrusion_allowed=False,
            )
        ]
    if failure == "coverage":
        stations.inspect_boundary_placement.return_value = [
            placement_inspection(
                fully_covered=False,
                edge_aligned=True,
                protrusion_allowed=False,
            )
        ]
    if failure == "edge":
        stations.inspect_boundary_placement.return_value = [
            placement_inspection(edge_aligned=False, protrusion_allowed=False)
        ]
    if failure == "duplicate":
        stations.robots_with_other_map_stations.return_value = {robot_id}
    with pytest.raises(BaseException) as error:
        await call(service, credential, map_id, robot_id=robot_id)
    assert error.value.status_code == code
    stations.save_many.assert_not_called()


@pytest.mark.asyncio
async def test_only_robot_conflict_is_translated():
    service, credential, map_id, _, _, _, stations = setup_service()
    stations.save_many.side_effect = DockingStationRobotConflict()
    with pytest.raises(BaseException) as error:
        await call(service, credential, map_id)
    assert error.value.status_code == 409
    stations.save_many.side_effect = RuntimeError("unrelated failure")
    with pytest.raises(RuntimeError, match="unrelated failure"):
        await call(service, credential, map_id)


def test_legacy_docking_station_endpoint_is_removed():
    app = FastAPI()
    app.include_router(
        MapRouter(MapHandler(SimpleNamespace())).router,
        prefix="/api/v1/maps",
    )
    path = "/api/v1/maps/stations"
    assert path not in app.openapi()["paths"]


def assignment_service(role=GroupRole.ADMIN):
    group_id, map_id, station_id, robot_id = (uuid4() for _ in range(4))
    credential = Credential(
        id=uuid4(),
        email="assignment@example.com",
        status=UserStatus.ACTIVE,
        active_group_id=group_id,
    )
    map_repo = SimpleNamespace(
        get_by_id_and_group_for_update=AsyncMock(return_value=SimpleNamespace(id=map_id))
    )
    station = SimpleNamespace(id=station_id, robot_id=None)
    station_repo = SimpleNamespace(
        get_by_id_and_map_for_update=AsyncMock(return_value=station),
        assign_robot=AsyncMock(),
    )
    bot_repo = SimpleNamespace(
        get_by_ids_and_group_for_update=AsyncMock(
            return_value=[SimpleNamespace(id=robot_id, map_id=map_id)]
        )
    )
    permission = SimpleNamespace(
        get_group_member=AsyncMock(
            return_value=SimpleNamespace(role=role) if role else None
        ),
        is_action_executable=PermissionService.is_action_executable,
    )

    async def transaction(callback):
        return await callback(SimpleNamespace())

    registry = SimpleNamespace(
        map_repo=lambda: map_repo,
        docking_station_repo=lambda: station_repo,
        bot_repo=lambda: bot_repo,
        transaction_wrapper=transaction,
    )
    return (
        DockingStationService(registry, permission),
        credential,
        map_id,
        station_id,
        robot_id,
        map_repo,
        station_repo,
        bot_repo,
    )


async def assign(service, credential, map_id, station_id, robot_id, group_id=None):
    await service.assign_robot(
        map_id=map_id,
        station_id=station_id,
        robot_id=robot_id,
        group_id=group_id or credential.active_group_id,
        credential=credential,
        ctx=AppContext(
            trace_id=uuid4(), actor=credential.id, action=ASSIGN_DOCKING_STATION
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [GroupRole.ADMIN, GroupRole.OWNER])
async def test_assign_robot_success_and_idempotence(role):
    service, credential, map_id, station_id, robot_id, _, stations, _ = (
        assignment_service(role)
    )
    await assign(service, credential, map_id, station_id, robot_id)
    stations.assign_robot.assert_awaited_once()

    stations.assign_robot.reset_mock()
    stations.get_by_id_and_map_for_update.return_value.robot_id = robot_id
    await assign(service, credential, map_id, station_id, robot_id)
    stations.assign_robot.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role", [None, GroupRole.GUEST, GroupRole.MEMBER, GroupRole.MODERATOR]
)
async def test_assign_robot_denies_non_admin_roles(role):
    service, credential, map_id, station_id, robot_id, _, stations, _ = (
        assignment_service(role)
    )
    with pytest.raises(BaseException) as error:
        await assign(service, credential, map_id, station_id, robot_id)
    assert error.value.status_code == 403
    stations.assign_robot.assert_not_awaited()


@pytest.mark.asyncio
async def test_assign_robot_denies_inactive_group_membership_before_resource_access():
    service, credential, map_id, station_id, robot_id, maps, stations, bots = (
        assignment_service(role=None)
    )

    with pytest.raises(BaseException) as error:
        await assign(service, credential, map_id, station_id, robot_id)

    assert error.value.status_code == 403
    maps.get_by_id_and_group_for_update.assert_not_awaited()
    stations.get_by_id_and_map_for_update.assert_not_awaited()
    bots.get_by_ids_and_group_for_update.assert_not_awaited()
    stations.assign_robot.assert_not_awaited()


@pytest.mark.asyncio
async def test_assign_robot_rejects_foreign_active_group_before_resource_access():
    service, credential, map_id, station_id, robot_id, maps, stations, bots = (
        assignment_service()
    )

    with pytest.raises(BaseException) as error:
        await assign(
            service,
            credential,
            map_id,
            station_id,
            robot_id,
            group_id=uuid4(),
        )

    assert error.value.status_code == 403
    maps.get_by_id_and_group_for_update.assert_not_awaited()
    stations.get_by_id_and_map_for_update.assert_not_awaited()
    bots.get_by_ids_and_group_for_update.assert_not_awaited()
    stations.assign_robot.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["map", "station", "robot", "robot_map"])
async def test_assign_robot_hides_inaccessible_resources(failure):
    service, credential, map_id, station_id, robot_id, maps, stations, bots = (
        assignment_service()
    )
    if failure == "map":
        maps.get_by_id_and_group_for_update.return_value = None
    elif failure == "station":
        stations.get_by_id_and_map_for_update.return_value = None
    elif failure == "robot":
        bots.get_by_ids_and_group_for_update.return_value = []
    else:
        bots.get_by_ids_and_group_for_update.return_value[0].map_id = uuid4()

    with pytest.raises(BaseException) as error:
        await assign(service, credential, map_id, station_id, robot_id)
    assert error.value.status_code == 404
    stations.assign_robot.assert_not_awaited()


@pytest.mark.asyncio
async def test_assign_robot_conflicts_for_occupied_station_or_unique_race():
    service, credential, map_id, station_id, robot_id, _, stations, _ = (
        assignment_service()
    )
    stations.get_by_id_and_map_for_update.return_value.robot_id = uuid4()
    with pytest.raises(BaseException) as occupied:
        await assign(service, credential, map_id, station_id, robot_id)
    assert occupied.value.status_code == 409
    stations.assign_robot.assert_not_awaited()

    stations.get_by_id_and_map_for_update.return_value.robot_id = None
    stations.assign_robot.side_effect = DockingStationRobotConflict()
    with pytest.raises(BaseException) as unique_conflict:
        await assign(service, credential, map_id, station_id, robot_id)
    assert unique_conflict.value.status_code == 409


def test_assignment_request_requires_non_null_robot_uuid_and_endpoint_returns_204():
    for payload in ({}, {"robot_id": None}, {"robot_id": "bad"}):
        with pytest.raises(ValidationError):
            DockingStationRobotAssignmentDTO(**payload)

    app = FastAPI()
    app.include_router(
        MapRouter(
            MapHandler(SimpleNamespace(docking_station_service=SimpleNamespace()))
        ).router,
        prefix="/api/v1/maps",
    )
    operation = app.openapi()["paths"][
        "/api/v1/maps/{map_id}/docking/{docking_id}/"
    ]["put"]
    assert operation["responses"]["204"]["description"] == "Successful Response"
    assert operation["requestBody"]["required"] is True
    assert "422" in operation["responses"]


@pytest.mark.parametrize("field", ["id", "robot_id"])
def test_batch_duplicate_identifiers(field):
    item = {"geometry": polygon(), field: uuid4()}
    with pytest.raises(ValidationError):
        DockingStationsSaveDTO(map_id=uuid4(), data=[item, item])


def test_batch_limits_and_required_fields():
    assert DockingStationsSaveDTO(map_id=uuid4(), data=[]).data == []
    assert (
        len(
            DockingStationsSaveDTO(
                map_id=uuid4(), data=[{"geometry": polygon()}] * 100
            ).data
        )
        == 100
    )
    for payload in (
        {"map_id": uuid4()},
        {"data": []},
        {"map_id": "bad", "data": []},
        {"map_id": uuid4(), "data": None},
        {"map_id": uuid4(), "data": [], "extra": True},
        {"map_id": uuid4(), "data": [{"geometry": polygon()}] * 101},
    ):
        with pytest.raises(ValidationError):
            DockingStationsSaveDTO(**payload)
