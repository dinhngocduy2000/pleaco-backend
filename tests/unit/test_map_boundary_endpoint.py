import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.common.context import AppContext
from app.common.enum.context_actions import SAVE_MAP_BOUNDARY
from app.common.enum.map import MapBoundarySource
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import BadRequestException, ForbiddenException, NotFoundException
from app.common.middleware.auth_middleware import AuthMiddleware
from app.common.schemas.map import MapBoundarySaveDTO
from app.common.schemas.user import Credential
from app.core.rbac.permissions import PermissionService
from app.handler.map import MapHandler
from app.router.map import MapRouter
from app.repository.map import MapRepository
from app.repository.map_boundary import MapBoundaryRepository
from app.services.map import MapService


POLYGON = {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 0]]]}


def setup_service(role=GroupRole.ADMIN, exists=True):
    group_id, map_id = uuid4(), uuid4()
    credential = Credential(
        id=uuid4(), email="boundary@example.com", status=UserStatus.ACTIVE,
        active_group_id=group_id,
    )
    record = SimpleNamespace(id=map_id, dimension_x=Decimal("10.25"), dimension_y=Decimal("5.5"))
    maps = SimpleNamespace(get_by_id_and_group_for_update=AsyncMock(return_value=record if exists else None))
    now = datetime.now(timezone.utc)

    async def save(**kwargs):
        return dict(id=uuid4(), map_id=map_id, source=kwargs["source"],
                    geometry=json.loads(kwargs["geometry_json"]), created_at=now, updated_at=now)

    boundaries = SimpleNamespace(inspect_geometry=AsyncMock(return_value=(True, True)), upsert=AsyncMock(side_effect=save))

    async def transaction(callback):
        return await callback(SimpleNamespace())

    registry = SimpleNamespace(
        map_repo=lambda: maps, map_boundary_repo=lambda: boundaries,
        transaction_wrapper=AsyncMock(side_effect=transaction),
    )
    permissions = SimpleNamespace(
        get_group_member=AsyncMock(return_value=SimpleNamespace(role=role) if role else None),
        is_action_executable=PermissionService.is_action_executable,
    )
    return MapService(registry, permissions), credential, record, maps, boundaries


async def invoke(service, credential, map_id, **payload):
    return await service.save_boundary(
        boundary_save=MapBoundarySaveDTO(map_id=map_id, **payload),
        group_id=credential.active_group_id, credential=credential,
        ctx=AppContext(trace_id=uuid4(), action=SAVE_MAP_BOUNDARY, actor=credential.id),
    )


@pytest.mark.parametrize("payload", [
    {"map_id": "bad"}, {"source": "UNKNOWN"}, {"source": None},
    {"source": "CUSTOM"}, {"source": "TEACH_MODE", "geometry": None},
    {"unexpected": True},
    *[{"geometry": {**POLYGON, "type": kind}} for kind in ["Point", "LineString", "polygon", "Unknown"]],
    {"geometry": {**POLYGON, "extra": 1}},
    *[{"geometry": {"type": "Polygon", "coordinates": coords}} for coords in [
        [], [[]], [[[0, 0], [1, 0], [0, 0]]],
        [[[0, 0], [1, 0], [1, 1], [0, 1]]],
        [[[0, 0, 0]] * 4], [[[0]] * 4],
        [[[True, 0]] * 4], [[["1", 0]] * 4],
        [[[float("nan"), 0]] * 4], [[[float("inf"), 0]] * 4],
    ]],
])
def test_request_rejects_invalid_fields(payload):
    with pytest.raises(ValidationError):
        MapBoundarySaveDTO.model_validate({"map_id": str(uuid4()), **payload})


def test_map_id_is_required():
    with pytest.raises(ValidationError):
        MapBoundarySaveDTO.model_validate({})


@pytest.mark.asyncio
async def test_repository_query_scopes_and_locks_map_and_preserves_identity():
    from sqlalchemy.dialects import postgresql

    map_id, group_id = uuid4(), uuid4()
    ctx = AppContext(trace_id=uuid4(), action=SAVE_MAP_BOUNDARY)
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)))
    assert await MapRepository().get_by_id_and_group_for_update(session, map_id, group_id, ctx) is None
    statement = session.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert "maps.id =" in str(statement) and "maps.group_id =" in str(statement)
    assert "FOR UPDATE" in str(statement)
    assert map_id in statement.params.values() and group_id in statement.params.values()

    now = datetime.now(timezone.utc)
    row = dict(id=uuid4(), map_id=map_id, source=MapBoundarySource.CUSTOM,
               geometry=json.dumps(POLYGON), created_at=now, updated_at=now)
    session.execute.return_value = SimpleNamespace(mappings=lambda: SimpleNamespace(one=lambda: row))
    result = await MapBoundaryRepository().upsert(session, map_id, MapBoundarySource.CUSTOM, json.dumps(POLYGON), ctx)
    assert result["geometry"] == POLYGON
    sql = str(session.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
    update_clause = sql.split("DO UPDATE SET", 1)[1].split("RETURNING", 1)[0]
    assert "source = excluded.source" in update_clause
    assert "geometry = excluded.geometry" in update_clause
    assert "updated_at = now()" in update_clause
    assert "created_at" not in update_clause and "id =" not in update_clause


@pytest.mark.asyncio
async def test_missing_map_stops_before_geometry_or_persistence():
    service, credential, record, maps, boundaries = setup_service(exists=False)
    service._dimension_boundary = Mock(side_effect=AssertionError("Must not generate geometry"))
    with pytest.raises(NotFoundException, match="Map not found"):
        await invoke(service, credential, record.id)
    service._dimension_boundary.assert_not_called()
    boundaries.inspect_geometry.assert_not_awaited()
    boundaries.upsert.assert_not_awaited()
    assert maps.get_by_id_and_group_for_update.call_args.kwargs["group_id"] == credential.active_group_id


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [GroupRole.OWNER, GroupRole.ADMIN])
async def test_authorized_dimensions_ignore_supplied_coordinates(role):
    service, credential, record, _, boundaries = setup_service(role)
    result = await invoke(service, credential, record.id, geometry=POLYGON)
    assert result.source == MapBoundarySource.DIMENSIONS
    assert result.geometry.coordinates == [[(0, 0), (10.25, 0), (10.25, 5.5), (0, 5.5), (0, 0)]]
    boundaries.upsert.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [GroupRole.MODERATOR, GroupRole.MEMBER, GroupRole.GUEST, None])
async def test_unprivileged_roles_never_access_map(role):
    service, credential, record, maps, _ = setup_service(role)
    with pytest.raises(ForbiddenException):
        await invoke(service, credential, record.id)
    maps.get_by_id_and_group_for_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_group_required():
    service, credential, record, maps, _ = setup_service()
    credential.active_group_id = None
    with pytest.raises(ForbiddenException):
        await invoke(service, credential, record.id)
    maps.get_by_id_and_group_for_update.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("dimension", [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), Decimal("1e1000"), Decimal("1e-1000")])
async def test_invalid_dimensions_rejected(dimension):
    service, credential, record, _, boundaries = setup_service()
    record.dimension_x = dimension
    with pytest.raises(BadRequestException, match="dimensions"):
        await invoke(service, credential, record.id)
    boundaries.inspect_geometry.assert_not_awaited()
    boundaries.upsert.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["CUSTOM", "TEACH_MODE"])
async def test_custom_sources_preserve_geometry(source):
    service, credential, record, _, _ = setup_service()
    result = await invoke(service, credential, record.id, source=source, geometry=POLYGON)
    assert result.geometry.model_dump(mode="json") == POLYGON


@pytest.mark.asyncio
@pytest.mark.parametrize("checks", [(False, False), (True, False)])
async def test_invalid_spatial_result_never_writes(checks):
    service, credential, record, _, boundaries = setup_service()
    boundaries.inspect_geometry.return_value = checks
    with pytest.raises(BadRequestException):
        await invoke(service, credential, record.id, source="CUSTOM", geometry=POLYGON)
    boundaries.upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_contract_authentication_and_openapi():
    service, credential, record, maps, boundaries = setup_service()
    app = FastAPI()
    app.include_router(MapRouter(MapHandler(service)).router, prefix="/api/v1/maps")
    path = "/api/v1/maps/boundary"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(path, json={"map_id": str(record.id)})
        assert response.status_code == 401
        app.dependency_overrides[AuthMiddleware.auth_middleware] = lambda: credential
        response = await client.post(path, json={"map_id": str(record.id)})
        assert response.status_code == 200
        assert response.json()["message"] == "Map boundary saved"
        assert response.json()["statusCode"] == 200
        assert response.json()["data"]["geometry"]["type"] == "Polygon"
        for payload in [{"map_id": "bad"}, {"map_id": str(record.id), "source": "bad"},
                        {"map_id": str(record.id), "geometry": {**POLYGON, "type": "Point"}}]:
            assert (await client.post(path, json=payload)).status_code == 422
        maps.get_by_id_and_group_for_update.return_value = None
        response = await client.post(path, json={"map_id": str(uuid4())})
        assert response.status_code == 404 and response.json()["detail"] == "Map not found"
        boundaries.upsert.assert_awaited_once()
    schema = app.openapi()
    assert "200" in schema["paths"][path]["post"]["responses"]
    assert schema["components"]["schemas"]["PolygonGeometry"]["properties"]["type"]["const"] == "Polygon"
    assert schema["components"]["schemas"]["MapBoundarySaveDTO"]["properties"]["source"]["default"] == "DIMENSIONS"
