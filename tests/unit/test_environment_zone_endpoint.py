import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.common.context import AppContext
from app.common.enum.context_actions import CREATE_ENVIRONMENT_ZONES
from app.common.enum.environment_zone import EnvironmentZoneType
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
)
from app.common.middleware.auth_middleware import AuthMiddleware
from app.common.schemas.map import EnvironmentZonesCreateDTO
from app.common.schemas.user import Credential
from app.core.rbac.permissions import PermissionService
from app.handler.map import MapHandler
from app.router.map import MapRouter
from app.repository.environment_zone import EnvironmentZoneRepository
from app.services.map import MapService


def polygon(left=0, bottom=0, right=2, top=2):
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [left, bottom],
                [right, bottom],
                [right, top],
                [left, top],
                [left, bottom],
            ]
        ],
    }


def payload(map_id=None, geometries=None):
    return {
        "map_id": str(map_id or uuid4()),
        "zones": [
            {"type": "NO_GO", "geometry": geometry}
            for geometry in (geometries or [polygon()])
        ],
    }


def setup_service(role=GroupRole.ADMIN, exists=True):
    group_id, map_id = uuid4(), uuid4()
    credential = Credential(
        id=uuid4(),
        email="zones@example.com",
        status=UserStatus.ACTIVE,
        active_group_id=group_id,
    )
    map_record = SimpleNamespace(id=map_id)
    maps = SimpleNamespace(
        get_by_id_and_group_for_update=AsyncMock(
            return_value=map_record if exists else None
        )
    )
    zones = SimpleNamespace(
        inspect_boundary_coverage=AsyncMock(),
        find_batch_overlap=AsyncMock(return_value=None),
        find_existing_overlap=AsyncMock(return_value=None),
        create_many=AsyncMock(),
    )

    async def inspect(**kwargs):
        return True, [
            (index, True, True) for index, _ in enumerate(kwargs["geometry_jsons"])
        ]

    zones.inspect_boundary_coverage.side_effect = inspect

    async def transaction(callback):
        return await callback(SimpleNamespace())

    registry = SimpleNamespace(
        map_repo=lambda: maps,
        environment_zone_repo=lambda: zones,
        transaction_wrapper=AsyncMock(side_effect=transaction),
    )
    permissions = SimpleNamespace(
        get_group_member=AsyncMock(
            return_value=SimpleNamespace(role=role) if role else None
        ),
        is_action_executable=PermissionService.is_action_executable,
    )
    return MapService(registry, permissions), credential, map_record, maps, zones


async def invoke(service, credential, map_id, geometries=None):
    return await service.create_environment_zones(
        zones_create=EnvironmentZonesCreateDTO.model_validate(
            payload(map_id, geometries)
        ),
        group_id=credential.active_group_id,
        credential=credential,
        ctx=AppContext(
            trace_id=uuid4(),
            action=CREATE_ENVIRONMENT_ZONES,
            actor=credential.id,
        ),
    )


@pytest.mark.parametrize(
    "invalid_payload",
    [
        {},
        {"map_id": "bad", "zones": [{"type": "NO_GO", "geometry": polygon()}]},
        {"map_id": str(uuid4()), "zones": []},
        {
            "map_id": str(uuid4()),
            "zones": [{"type": "UNKNOWN", "geometry": polygon()}],
        },
        {
            "map_id": str(uuid4()),
            "zones": [{"type": "NO_GO", "geomatry": polygon()}],
        },
        {
            "map_id": str(uuid4()),
            "zones": [{"type": "NO_GO", "geometry": polygon(), "extra": True}],
        },
        {
            "map_id": str(uuid4()),
            "zones": [{"type": "NO_GO", "geometry": polygon()}],
            "extra": True,
        },
        {
            "map_id": str(uuid4()),
            "zones": [
                {
                    "type": "NO_GO",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1]]],
                    },
                }
            ],
        },
    ],
)
def test_request_rejects_invalid_fields(invalid_payload):
    with pytest.raises(ValidationError):
        EnvironmentZonesCreateDTO.model_validate(invalid_payload)


def test_request_accepts_one_to_one_hundred_zones_and_all_types():
    request = EnvironmentZonesCreateDTO.model_validate(
        {
            "map_id": str(uuid4()),
            "zones": [
                {"type": zone_type.value, "geometry": polygon()}
                for zone_type in EnvironmentZoneType
            ],
        }
    )
    assert [zone.type for zone in request.zones] == list(EnvironmentZoneType)

    hundred = payload(geometries=[polygon()] * 100)
    assert len(EnvironmentZonesCreateDTO.model_validate(hundred).zones) == 100
    with pytest.raises(ValidationError):
        EnvironmentZonesCreateDTO.model_validate(payload(geometries=[polygon()] * 101))


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [GroupRole.OWNER, GroupRole.ADMIN])
async def test_owner_and_admin_create_an_atomic_batch(role):
    service, credential, map_record, maps, zones = setup_service(role)
    geometries = [polygon(0, 0, 2, 2), polygon(2, 0, 4, 2)]

    assert await invoke(service, credential, map_record.id, geometries) is None

    maps.get_by_id_and_group_for_update.assert_awaited_once()
    assert (
        maps.get_by_id_and_group_for_update.call_args.kwargs["group_id"]
        == credential.active_group_id
    )
    zones.create_many.assert_awaited_once()
    saved = zones.create_many.call_args.kwargs["zones"]
    assert [zone_type for zone_type, _ in saved] == [
        EnvironmentZoneType.NO_GO,
        EnvironmentZoneType.NO_GO,
    ]
    assert [json.loads(geometry) for _, geometry in saved] == geometries


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role", [GroupRole.MODERATOR, GroupRole.MEMBER, GroupRole.GUEST, None]
)
async def test_unprivileged_roles_never_access_the_map(role):
    service, credential, map_record, maps, _ = setup_service(role)
    with pytest.raises(ForbiddenException):
        await invoke(service, credential, map_record.id)
    maps.get_by_id_and_group_for_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_group_is_required_before_map_access():
    service, credential, map_record, maps, _ = setup_service()
    credential.active_group_id = None
    with pytest.raises(ForbiddenException):
        await invoke(service, credential, map_record.id)
    maps.get_by_id_and_group_for_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_or_cross_group_map_stops_before_zone_validation():
    service, credential, map_record, _, zones = setup_service(exists=False)
    with pytest.raises(NotFoundException, match="Map not found"):
        await invoke(service, credential, map_record.id)
    zones.inspect_boundary_coverage.assert_not_awaited()
    zones.create_many.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inspection", "message"),
    [
        ((False, [(0, True, False)]), "boundary"),
        ((True, [(0, False, False)]), "valid"),
        ((True, [(0, True, False)]), "within"),
    ],
)
async def test_boundary_and_geometry_failures_never_write(inspection, message):
    service, credential, map_record, _, zones = setup_service()
    zones.inspect_boundary_coverage.side_effect = None
    zones.inspect_boundary_coverage.return_value = inspection
    with pytest.raises(BadRequestException, match=message):
        await invoke(service, credential, map_record.id)
    zones.find_batch_overlap.assert_not_awaited()
    zones.create_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_intra_batch_overlap_never_checks_existing_or_writes():
    service, credential, map_record, _, zones = setup_service()
    zones.find_batch_overlap.return_value = (0, 1)
    with pytest.raises(BadRequestException, match="indexes 0 and 1"):
        await invoke(
            service,
            credential,
            map_record.id,
            [polygon(0, 0, 3, 3), polygon(1, 1, 2, 2)],
        )
    zones.find_existing_overlap.assert_not_awaited()
    zones.create_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_overlap_never_writes():
    service, credential, map_record, _, zones = setup_service()
    zones.find_existing_overlap.return_value = 0
    with pytest.raises(BadRequestException, match="index 0"):
        await invoke(service, credential, map_record.id)
    zones.create_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_repository_uses_set_based_postgis_queries_and_bulk_insert():
    class Result:
        def all(self):
            return [
                SimpleNamespace(
                    zone_index=0,
                    boundary_exists=True,
                    valid=True,
                    covered=True,
                ),
                SimpleNamespace(
                    zone_index=1,
                    boundary_exists=True,
                    valid=True,
                    covered=True,
                ),
            ]

        def one_or_none(self):
            return None

        def scalar_one_or_none(self):
            return None

    class Session:
        def __init__(self):
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            return Result()

    repository = EnvironmentZoneRepository()
    session = Session()
    map_id = uuid4()
    geometries = [json.dumps(polygon()), json.dumps(polygon(2, 0, 4, 2))]
    ctx = AppContext(trace_id=uuid4(), action=CREATE_ENVIRONMENT_ZONES)

    assert await repository.inspect_boundary_coverage(
        session, map_id, geometries, ctx
    ) == (True, [(0, True, True), (1, True, True)])
    assert await repository.find_batch_overlap(session, geometries, ctx) is None
    assert (
        await repository.find_existing_overlap(session, map_id, geometries, ctx) is None
    )
    await repository.create_many(
        session,
        map_id,
        [
            (EnvironmentZoneType.NO_GO, geometries[0]),
            (EnvironmentZoneType.OBSTACLE, geometries[1]),
        ],
        ctx,
    )

    statements = [
        statement.compile(dialect=postgresql.dialect())
        for statement in session.statements
    ]
    coverage_sql, batch_sql, existing_sql, insert_sql = map(str, statements)
    assert "UNION ALL" in coverage_sql and "ST_Covers" in coverage_sql
    assert "ST_IsValid" in coverage_sql and "ST_IsEmpty" in coverage_sql
    assert "ST_Relate" in batch_sql and "left_zone.zone_index <" in batch_sql
    assert "ST_Relate" in existing_sql and "JOIN environment_zones" in existing_sql
    assert "INSERT INTO environment_zones" in insert_sql
    assert insert_sql.count("ST_SetSRID(ST_GeomFromGeoJSON") == 2
    assert any(
        "T********" in values.values()
        for values in [statement.params for statement in statements]
    )


@pytest.mark.asyncio
async def test_http_contract_authentication_validation_and_openapi():
    service, credential, map_record, _, _ = setup_service()
    app = FastAPI()
    app.include_router(MapRouter(MapHandler(service)).router, prefix="/api/v1/maps")
    path = "/api/v1/maps/zones"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(path, json=payload(map_record.id))
        assert response.status_code == 401

        app.dependency_overrides[AuthMiddleware.auth_middleware] = lambda: credential
        response = await client.post(path, json=payload(map_record.id))
        assert response.status_code == 204
        assert response.content == b""

        for invalid_payload in [
            {"map_id": str(map_record.id), "zones": []},
            {
                "map_id": str(map_record.id),
                "zones": [{"type": "UNKNOWN", "geometry": polygon()}],
            },
        ]:
            assert (await client.post(path, json=invalid_payload)).status_code == 422

    operation = app.openapi()["paths"][path]["post"]
    assert "204" in operation["responses"]
    assert "content" not in operation["responses"]["204"]
