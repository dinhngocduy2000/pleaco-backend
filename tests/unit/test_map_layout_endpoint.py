from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.common.context import AppContext
from app.common.enum.context_actions import SAVE_MAP_LAYOUT
from app.common.enum.map import MapBoundarySource
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import BadRequestException, ForbiddenException
from app.common.middleware.auth_middleware import AuthMiddleware
from app.common.schemas.map import MapLayoutSaveDTO
from app.common.schemas.user import Credential
from app.core.rbac.permissions import PermissionService
from app.handler.map import MapHandler
from app.router.map import MapRouter
from app.services.map import MapService


POLYGON = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [4, 0], [4, 4], [0, 0]]],
}


def permissions(role: GroupRole = GroupRole.ADMIN):
    return SimpleNamespace(
        get_group_member=AsyncMock(return_value=SimpleNamespace(role=role)),
        is_action_executable=PermissionService.is_action_executable,
    )


def setup_service(
    *,
    docking_error: Exception | None = None,
    role: GroupRole = GroupRole.ADMIN,
):
    group_id, map_id = uuid4(), uuid4()
    credential = Credential(
        id=uuid4(),
        email="layout@example.com",
        status=UserStatus.ACTIVE,
        active_group_id=group_id,
    )
    map_record = SimpleNamespace(
        id=map_id,
        dimension_x=Decimal("10"),
        dimension_y=Decimal("10"),
    )
    map_repository = SimpleNamespace(
        get_by_id_and_group_for_update=AsyncMock(return_value=map_record)
    )
    order: list[str] = []
    now = datetime.now(timezone.utc)

    async def upsert(**kwargs):
        order.append("boundary")
        return {
            "id": uuid4(),
            "map_id": map_id,
            "source": kwargs["source"],
            "geometry": POLYGON,
            "created_at": now,
            "updated_at": now,
        }

    boundary_repository = SimpleNamespace(
        inspect_geometry=AsyncMock(return_value=(True, True)),
        upsert=AsyncMock(side_effect=upsert),
    )
    transaction_state = SimpleNamespace(committed=False, rolled_back=False)
    transaction_session = object()

    async def transaction(callback):
        try:
            result = await callback(transaction_session)
        except Exception:
            transaction_state.rolled_back = True
            raise
        transaction_state.committed = True
        return result

    registry = SimpleNamespace(
        map_repo=lambda: map_repository,
        map_boundary_repo=lambda: boundary_repository,
        transaction_wrapper=AsyncMock(side_effect=transaction),
    )

    async def save_zones(**kwargs):
        assert kwargs["session"] is transaction_session
        order.append("zones")

    async def save_stations(**kwargs):
        assert kwargs["session"] is transaction_session
        order.append("stations")
        if docking_error is not None:
            raise docking_error
        return []

    async def validate_zones(**kwargs):
        assert kwargs["session"] is transaction_session
        order.append("validate_zones")

    async def validate_stations(**kwargs):
        assert kwargs["session"] is transaction_session
        order.append("validate_stations")

    zones_service = SimpleNamespace(
        save_environment_zones_in_transaction=AsyncMock(side_effect=save_zones),
        validate_all_within_boundary=AsyncMock(side_effect=validate_zones),
    )
    docking_service = SimpleNamespace(
        save_docking_stations_in_transaction=AsyncMock(side_effect=save_stations),
        validate_all_within_boundary=AsyncMock(side_effect=validate_stations),
    )
    service = MapService(
        repo=registry,
        permission_service=permissions(role),
        environment_zones_service=zones_service,
        docking_station_service=docking_service,
    )
    return SimpleNamespace(
        service=service,
        credential=credential,
        map_id=map_id,
        maps=map_repository,
        registry=registry,
        zones=zones_service,
        docking=docking_service,
        order=order,
        transaction=transaction_state,
    )


def request(map_id, **sections) -> MapLayoutSaveDTO:
    return MapLayoutSaveDTO.model_validate({"map_id": str(map_id), **sections})


async def save_layout(db, payload: MapLayoutSaveDTO) -> None:
    await db.service.save_layout(
        layout_save=payload,
        group_id=db.credential.active_group_id,
        credential=db.credential,
        ctx=AppContext(
            trace_id=uuid4(), action=SAVE_MAP_LAYOUT, actor=db.credential.id
        ),
    )


def test_schema_supports_partial_null_and_empty_sections():
    map_id = uuid4()
    empty = request(map_id)
    assert empty.boundary is None
    assert empty.environment_zones is None
    assert empty.docking_stations is None

    explicit_null = request(
        map_id,
        boundary=None,
        environment_zones=None,
        docking_stations=None,
    )
    assert explicit_null == empty
    assert request(map_id, environment_zones=[]).environment_zones == []
    assert request(map_id, docking_stations=[]).docking_stations == []


def test_schema_rejects_nested_map_id_duplicates_and_oversized_batches():
    map_id, item_id = uuid4(), uuid4()
    with pytest.raises(ValidationError):
        request(map_id, boundary={"map_id": str(map_id)})
    with pytest.raises(ValidationError):
        request(
            map_id,
            environment_zones=[
                {"id": str(item_id), "type": "NO_GO", "geometry": POLYGON},
                {"id": str(item_id), "type": "OBSTACLE", "geometry": POLYGON},
            ],
        )
    with pytest.raises(ValidationError):
        request(
            map_id,
            docking_stations=[{"geometry": POLYGON}] * 101,
        )


@pytest.mark.asyncio
async def test_service_uses_one_transaction_and_required_order():
    db = setup_service()
    await save_layout(
        db,
        request(
            db.map_id,
            boundary={"source": MapBoundarySource.CUSTOM, "geometry": POLYGON},
            environment_zones=[],
            docking_stations=[],
        ),
    )

    assert db.order == [
        "boundary",
        "zones",
        "stations",
        "validate_zones",
        "validate_stations",
    ]
    db.registry.transaction_wrapper.assert_awaited_once()
    db.maps.get_by_id_and_group_for_update.assert_awaited_once()
    assert db.transaction.committed is True
    assert db.transaction.rolled_back is False


@pytest.mark.asyncio
async def test_map_only_request_is_an_authorized_no_op():
    db = setup_service()
    await save_layout(db, request(db.map_id))

    db.maps.get_by_id_and_group_for_update.assert_awaited_once()
    db.zones.save_environment_zones_in_transaction.assert_not_awaited()
    db.docking.save_docking_stations_in_transaction.assert_not_awaited()
    assert db.order == []
    assert db.transaction.committed is True


@pytest.mark.asyncio
async def test_later_failure_rolls_back_the_layout_operation():
    db = setup_service(docking_error=RuntimeError("station failure"))
    with pytest.raises(RuntimeError, match="station failure"):
        await save_layout(
            db,
            request(
                db.map_id,
                boundary={"source": "CUSTOM", "geometry": POLYGON},
                environment_zones=[],
                docking_stations=[],
            ),
        )

    assert db.order == ["boundary", "zones", "stations"]
    assert db.transaction.committed is False
    assert db.transaction.rolled_back is True


@pytest.mark.asyncio
async def test_final_layout_validation_failure_rolls_back_all_changes():
    db = setup_service()
    db.zones.validate_all_within_boundary.side_effect = BadRequestException(
        message="All environment zones must be within the map boundary"
    )

    with pytest.raises(BadRequestException, match="within the map boundary"):
        await save_layout(
            db,
            request(
                db.map_id,
                boundary={"source": "CUSTOM", "geometry": POLYGON},
                docking_stations=[],
            ),
        )

    db.docking.validate_all_within_boundary.assert_not_awaited()
    assert db.transaction.committed is False
    assert db.transaction.rolled_back is True


@pytest.mark.asyncio
async def test_layout_requires_admin_or_owner_role():
    db = setup_service(role=GroupRole.MODERATOR)

    with pytest.raises(ForbiddenException):
        await save_layout(db, request(db.map_id))

    db.registry.transaction_wrapper.assert_not_awaited()
    db.maps.get_by_id_and_group_for_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_contract_and_openapi():
    db = setup_service()
    app = FastAPI()
    app.include_router(
        MapRouter(MapHandler(db.service, db.zones, db.docking)).router,
        prefix="/api/v1/maps",
    )
    path = "/api/v1/maps/layouts"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.post(path, json={"map_id": str(db.map_id)})
        ).status_code == 401
        app.dependency_overrides[AuthMiddleware.auth_middleware] = lambda: db.credential
        response = await client.post(
            path,
            json={
                "map_id": str(db.map_id),
                "boundary": None,
                "environment_zones": None,
                "docking_stations": None,
            },
        )
        assert response.status_code == 204
        assert response.content == b""

    operation = app.openapi()["paths"][path]["post"]
    assert {"204", "400", "401", "403", "404", "409", "422"} <= operation[
        "responses"
    ].keys()
    schema = app.openapi()["components"]["schemas"]["MapLayoutSaveDTO"]
    assert schema["required"] == ["map_id"]
    boundary_schema = app.openapi()["components"]["schemas"]["MapLayoutBoundarySaveDTO"]
    assert "map_id" not in boundary_schema["properties"]
