from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.common.context import AppContext
from app.common.enum.context_actions import CREATE_DOCKING_STATION
from app.common.enum.docking_station import DockingStationHeading
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import BaseException
from app.common.middleware.auth_middleware import AuthMiddleware
from app.common.schemas.map import DockingStationCreateDTO
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
        get_by_id_and_group_for_update=AsyncMock(
            return_value=SimpleNamespace(id=robot_id, map_id=map_id)
        )
    )
    now = datetime.now(timezone.utc)

    async def create(**kwargs):
        return dict(
            id=uuid4(),
            map_id=kwargs["map_id"],
            robot_id=kwargs["robot_id"],
            heading=kwargs["heading"],
            geometry=polygon(),
            created_at=now,
            updated_at=now,
        )

    stations = SimpleNamespace(
        inspect_boundary_coverage=AsyncMock(return_value=(True, True, True)),
        robot_has_station=AsyncMock(return_value=False),
        create=AsyncMock(side_effect=create),
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
    return await service.create_docking_station(
        map_id=map_id,
        station_create=DockingStationCreateDTO(geometry=polygon(), **payload),
        group_id=credential.active_group_id,
        credential=credential,
        ctx=AppContext(
            trace_id=uuid4(), actor=credential.id, action=CREATE_DOCKING_STATION
        ),
    )


@pytest.mark.parametrize("heading", [None, *DockingStationHeading])
def test_heading_normalization(heading):
    dto = DockingStationCreateDTO(geometry=polygon(), heading=heading)
    assert dto.heading == (heading or DockingStationHeading.SOUTH)
    assert (
        DockingStationCreateDTO(geometry=polygon()).heading
        == DockingStationHeading.SOUTH
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", list(GroupRole) + [None])
async def test_roles(role):
    service, credential, map_id, _, _, _, stations = setup_service(role)
    if role in (GroupRole.OWNER, GroupRole.ADMIN):
        result = await call(service, credential, map_id)
        assert result.heading == DockingStationHeading.SOUTH
        assert result.robot_id is None
    else:
        with pytest.raises(BaseException) as error:
            await call(service, credential, map_id)
        assert error.value.status_code == 403
        stations.create.assert_not_called()


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
        bots.get_by_id_and_group_for_update.return_value = None
    if failure == "robot_map":
        bots.get_by_id_and_group_for_update.return_value.map_id = uuid4()
    if failure == "boundary":
        stations.inspect_boundary_coverage.return_value = (False, True, False)
    if failure == "topology":
        stations.inspect_boundary_coverage.return_value = (True, False, False)
    if failure == "coverage":
        stations.inspect_boundary_coverage.return_value = (True, True, False)
    if failure == "duplicate":
        stations.robot_has_station.return_value = True
    with pytest.raises(BaseException) as error:
        await call(service, credential, map_id, robot_id=robot_id)
    assert error.value.status_code == code
    stations.create.assert_not_called()


@pytest.mark.asyncio
async def test_only_robot_conflict_is_translated():
    service, credential, map_id, _, _, _, stations = setup_service()
    stations.create.side_effect = DockingStationRobotConflict()
    with pytest.raises(BaseException) as error:
        await call(service, credential, map_id)
    assert error.value.status_code == 409
    stations.create.side_effect = RuntimeError("unrelated failure")
    with pytest.raises(RuntimeError, match="unrelated failure"):
        await call(service, credential, map_id)


@pytest.mark.asyncio
async def test_http_contract_and_openapi():
    service, credential, map_id, robot_id, _, _, _ = setup_service()
    app = FastAPI()
    app.include_router(
        MapRouter(MapHandler(SimpleNamespace(), SimpleNamespace(), service)).router,
        prefix="/api/v1/maps",
    )
    path = f"/api/v1/maps/{map_id}/stations"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.post(path, json={"geometry": polygon()})
        ).status_code == 401
        app.dependency_overrides[AuthMiddleware.auth_middleware] = lambda: credential
        for extra in (
            {},
            {"heading": None},
            *(
                {"heading": h.value, "robot_id": str(robot_id)}
                for h in DockingStationHeading
            ),
        ):
            response = await client.post(path, json={"geometry": polygon(), **extra})
            assert response.status_code == 201
            body = response.json()
            assert body["message"] == "Docking station created"
            assert body["statusCode"] == 201
            assert set(body["data"]) == {
                "id",
                "map_id",
                "robot_id",
                "geometry",
                "heading",
                "created_at",
                "updated_at",
            }
            assert body["data"]["heading"] == (extra.get("heading") or "SOUTH")
            assert body["data"]["robot_id"] == extra.get("robot_id")
            assert body["data"]["geometry"] == polygon()
        for payload in (
            {},
            {"geometry": None},
            {"geometry": polygon(), "heading": "NORTHEAST"},
            {"geometry": polygon(), "robot_id": "bad"},
            {"geometry": polygon(), "map_id": str(map_id)},
            {"geometry": {"type": "Point", "coordinates": [0, 0]}},
            {
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1]]],
                }
            },
        ):
            assert (await client.post(path, json=payload)).status_code == 422
        assert (
            await client.post("/api/v1/maps/bad/stations", json={"geometry": polygon()})
        ).status_code == 422
    operation = app.openapi()["paths"]["/api/v1/maps/{map_id}/stations"]["post"]
    assert {"201", "400", "401", "403", "404", "409", "422"} <= operation[
        "responses"
    ].keys()
    schema = app.openapi()["components"]["schemas"]["DockingStationCreateDTO"]
    assert schema["required"] == ["geometry"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["heading"]["default"] == "SOUTH"
