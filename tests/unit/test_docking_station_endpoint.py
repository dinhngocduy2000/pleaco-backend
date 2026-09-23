from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI

from app.common.context import AppContext
from app.common.enum.context_actions import SAVE_DOCKING_STATIONS
from app.common.enum.docking_station import DockingStationHeading
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import BaseException
from app.common.schemas.map import DockingStationSaveItemDTO, DockingStationsSaveDTO
from pydantic import ValidationError
from app.common.schemas.user import Credential
from app.core.rbac.permissions import PermissionService
from app.handler.map import MapHandler
from app.repository.docking_station import DockingStationRobotConflict
from app.router.map import MapRouter
from app.services.docking_station import DockingStationService


def polygon():
    return {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 0]]]}


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
        inspect_boundary_coverage=AsyncMock(return_value=[(True, True, True)]),
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
        stations.inspect_boundary_coverage.return_value = [(False, True, False)]
    if failure == "topology":
        stations.inspect_boundary_coverage.return_value = [(True, False, False)]
    if failure == "coverage":
        stations.inspect_boundary_coverage.return_value = [(True, True, False)]
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
