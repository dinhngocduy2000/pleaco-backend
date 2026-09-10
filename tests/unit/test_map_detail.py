import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI

from app.common.context import AppContext
from app.common.enum.context_actions import LIST_MAPS
from app.common.enum.environment_zone import EnvironmentZoneType
from app.common.enum.map import MapStatus
from app.common.enum.robot import (
    RobotConnectionStatus,
    RobotModel,
    RobotOperationalStatus,
)
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import ForbiddenException, NotFoundException
from app.common.schemas.group import GroupMemberInfo
from app.common.schemas.user import Credential
from app.handler.map import MapHandler
from app.repository.map import MapRepository
from app.router.map import MapRouter
from app.services.map import MapService


def _ctx(actor_id: UUID | None = None) -> AppContext:
    return AppContext(trace_id=uuid4(), action=LIST_MAPS, actor=actor_id)


def _credential(active_group_id: UUID | None) -> Credential:
    return Credential(
        id=uuid4(),
        email="member@example.com",
        status=UserStatus.ACTIVE,
        active_group_id=active_group_id,
    )


class PermissionServiceStub:
    def __init__(self, role: GroupRole | None) -> None:
        self.role = role

    async def get_group_member(self, credential, ctx, group_id=None):
        if self.role is None or group_id is None:
            return None
        now = datetime.now(timezone.utc)
        return GroupMemberInfo(
            member_id=credential.id,
            group_id=group_id,
            role=self.role,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def is_action_executable(role, action, is_owner=False):
        return action == LIST_MAPS


class MapRepositoryStub:
    def __init__(self, detail: dict | None) -> None:
        self.detail = detail
        self.calls: list[tuple[UUID, UUID]] = []

    async def get_detail_by_id_and_group(self, *, map_id, group_id, **kwargs):
        self.calls.append((map_id, group_id))
        return self.detail


def _detail(map_id: UUID) -> dict:
    now = datetime.now(timezone.utc)
    boundary = {
        "type": "Polygon",
        "coordinates": [
            [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
            [[1, 1], [1, 2], [2, 2], [2, 1], [1, 1]],
        ],
    }
    return {
        "id": map_id,
        "name": "Floor 1",
        "description": "Main floor",
        "status": MapStatus.ASSIGNED,
        "dimension_x": Decimal("20"),
        "dimension_y": Decimal("30"),
        "created_at": now,
        "updated_at": now,
        "boundary": boundary,
        "tags": [{"id": uuid4(), "name": "Operations"}],
        "robots": [
            {
                "id": uuid4(),
                "name": "Scrubber 01",
                "serial_num": "SN-001",
                "model": RobotModel.STANDARD,
                "connection_status": RobotConnectionStatus.ONLINE,
                "operational_status": RobotOperationalStatus.IDLE,
            }
        ],
        "zones": [
            {
                "id": uuid4(),
                "type": EnvironmentZoneType.NO_GO,
                "geometry": boundary,
            }
        ],
    }


def _service(role: GroupRole | None, detail: dict | None):
    map_repository = MapRepositoryStub(detail)
    registry = SimpleNamespace(
        map_repo=lambda: map_repository,
        transaction_wrapper=lambda callback: callback(SimpleNamespace()),
    )
    return MapService(registry, PermissionServiceStub(role)), map_repository


@pytest.mark.asyncio
@pytest.mark.parametrize("role", list(GroupRole))
async def test_every_group_role_can_get_active_group_map_detail(role: GroupRole) -> None:
    group_id, map_id = uuid4(), uuid4()
    service, repository = _service(role, _detail(map_id))
    credential = _credential(group_id)

    detail = await service.get_map_detail(
        map_id=map_id,
        group_id=group_id,
        credential=credential,
        ctx=_ctx(credential.id),
    )

    assert repository.calls == [(map_id, group_id)]
    serialized = detail.model_dump(mode="json")
    assert "group_id" not in serialized
    assert serialized["boundary"]["coordinates"][1][0] == [1.0, 1.0]
    assert set(serialized["tags"][0]) == {"id", "name"}
    assert set(serialized["robots"][0]) == {
        "id",
        "name",
        "serial_num",
        "model",
        "connection_status",
        "operational_status",
    }


@pytest.mark.asyncio
async def test_map_detail_rejects_non_members_and_missing_active_group_before_lookup() -> None:
    group_id, map_id = uuid4(), uuid4()
    non_member, non_member_repo = _service(None, _detail(map_id))
    with pytest.raises(ForbiddenException):
        await non_member.get_map_detail(
            map_id=map_id,
            group_id=group_id,
            credential=_credential(group_id),
            ctx=_ctx(),
        )
    assert non_member_repo.calls == []

    no_group, no_group_repo = _service(GroupRole.MEMBER, _detail(map_id))
    with pytest.raises(ForbiddenException):
        await no_group.get_map_detail(
            map_id=map_id,
            group_id=None,
            credential=_credential(None),
            ctx=_ctx(),
        )
    assert no_group_repo.calls == []


@pytest.mark.asyncio
async def test_map_detail_returns_not_found_for_missing_or_cross_group_map() -> None:
    group_id, map_id = uuid4(), uuid4()
    service, repository = _service(GroupRole.MEMBER, None)
    with pytest.raises(NotFoundException, match="Map not found"):
        await service.get_map_detail(
            map_id=map_id,
            group_id=group_id,
            credential=_credential(group_id),
            ctx=_ctx(),
        )
    assert repository.calls == [(map_id, group_id)]


@pytest.mark.asyncio
async def test_map_detail_allows_absent_related_records() -> None:
    map_id, group_id = uuid4(), uuid4()
    detail = _detail(map_id)
    detail.update(boundary=None, tags=[], robots=[], zones=[])
    service, _ = _service(GroupRole.GUEST, detail)

    result = await service.get_map_detail(
        map_id=map_id,
        group_id=group_id,
        credential=_credential(group_id),
        ctx=_ctx(),
    )
    assert result.boundary is None
    assert result.tags == []
    assert result.robots == []
    assert result.zones == []


class ResultStub:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def one_or_none(self):
        return None if not self.rows else self.rows[0]

    def all(self):
        return self.rows


class SessionStub:
    def __init__(self, result_rows):
        self.result_rows = iter(result_rows)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return ResultStub(next(self.result_rows))


@pytest.mark.asyncio
async def test_detail_repository_scopes_and_decodes_ordered_related_data() -> None:
    map_id, group_id = uuid4(), uuid4()
    geometry = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
    session = SessionStub(
        [
            [{
                "id": map_id,
                "name": "Floor 1",
                "description": None,
                "status": MapStatus.UNASSIGNED,
                "dimension_x": Decimal("1"),
                "dimension_y": Decimal("1"),
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
                "boundary": json.dumps(geometry),
            }],
            [{"id": uuid4(), "name": "Alpha"}],
            [{
                "id": uuid4(), "name": "Bot A", "serial_num": "SN-A",
                "model": RobotModel.LITE,
                "connection_status": RobotConnectionStatus.OFFLINE,
                "operational_status": RobotOperationalStatus.CHARGING,
            }],
            [{"id": uuid4(), "type": EnvironmentZoneType.OBSTACLE, "geometry": json.dumps(geometry)}],
        ]
    )

    detail = await MapRepository().get_detail_by_id_and_group(
        session=session, map_id=map_id, group_id=group_id, ctx=_ctx()
    )

    assert detail["boundary"] == geometry
    assert detail["zones"][0]["geometry"] == geometry
    statements = [str(statement) for statement in session.statements]
    assert "maps.id" in statements[0] and "maps.group_id" in statements[0]
    assert "LEFT OUTER JOIN map_boundaries" in statements[0]
    assert "ST_AsGeoJSON(map_boundaries.geometry" in statements[0]
    assert "ORDER BY tags.name ASC, tags.id ASC" in statements[1]
    assert "ORDER BY robots.name ASC, robots.id ASC" in statements[2]
    assert "ORDER BY environment_zones.created_at ASC, environment_zones.id ASC" in statements[3]


def test_router_declares_map_detail_contract_and_openapi_path() -> None:
    router = MapRouter(MapHandler(SimpleNamespace())).router
    route = next(
        route
        for route in router.routes
        if getattr(route, "path", None) == "/{map_id}" and "GET" in route.methods
    )
    assert route.status_code == 200
    assert route.response_model.__name__ == "BaseResponse[MapDetailInfo]"

    app = FastAPI()
    app.include_router(router, prefix="/api/v1/maps")
    operation = app.openapi()["paths"]["/api/v1/maps/{map_id}"]["get"]
    assert operation["parameters"][0]["schema"]["format"] == "uuid"
    assert "200" in operation["responses"]
