from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.common.context import AppContext
from app.common.enum.context_actions import LIST_MAPS
from app.common.enum.map import MapStatus
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import ForbiddenException
from app.common.schemas.group import GroupMemberInfo
from app.common.schemas.map import MapListQuery, MapOrderDirection
from app.common.schemas.user import Credential
from app.models.tag import Tag
from app.repository.map import MapRepository
from app.repository.map_tags import MapTagsRepository
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
    def __init__(self) -> None:
        self.map_id = uuid4()
        self.group_id = None
        self.query = None

    async def list_maps(self, *, query, group_id, **kwargs):
        self.query = query
        self.group_id = group_id
        return [
            {
                "id": self.map_id,
                "name": "Floor 1",
                "description": "Main floor",
                "status": MapStatus.UNASSIGNED,
                "dimension_x": "20",
                "dimension_y": "30",
            }
        ], 1


class MapTagsRepositoryStub:
    def __init__(self) -> None:
        self.group_id = None

    async def get_by_map_ids(self, *, map_ids, group_id, **kwargs):
        self.group_id = group_id
        now = datetime.now(timezone.utc)
        tag = Tag(
            id=uuid4(),
            group_id=group_id,
            name="Operations",
            description=None,
            color="#336699",
        )
        tag.created_at = now
        tag.updated_at = now
        return {map_id: [tag] for map_id in map_ids}


def _service(role: GroupRole | None):
    map_repository = MapRepositoryStub()
    map_tags_repository = MapTagsRepositoryStub()
    registry = SimpleNamespace(
        map_repo=lambda: map_repository,
        map_tags_repo=lambda: map_tags_repository,
        transaction_wrapper=lambda callback: callback(SimpleNamespace()),
    )
    return MapService(registry, PermissionServiceStub(role)), map_repository, map_tags_repository


def test_map_list_query_defaults_and_validation() -> None:
    query = MapListQuery()
    tag_id = uuid4()

    assert query.page == 1
    assert query.page_size == 10
    assert query.order_direction == MapOrderDirection.DESC
    assert query.status is None

    invalid_queries = [
        {"page_size": 101},
        {"tag_ids": [tag_id, tag_id]},
        {"group_id": uuid4()},
    ]
    for values in invalid_queries:
        with pytest.raises(ValidationError):
            MapListQuery(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", list(GroupRole))
async def test_all_accepted_roles_list_only_active_group_maps(role: GroupRole) -> None:
    active_group_id = uuid4()
    credential = _credential(active_group_id)
    service, map_repository, map_tags_repository = _service(role)
    query = MapListQuery(search="floor", status=MapStatus.UNASSIGNED)

    maps, total = await service.list_maps(
        query=query,
        group_id=credential.active_group_id,
        credential=credential,
        ctx=_ctx(credential.id),
    )

    assert total == 1
    assert map_repository.group_id == active_group_id
    assert map_tags_repository.group_id == active_group_id
    assert maps[0].name == "Floor 1"
    assert maps[0].tags[0].name == "Operations"


@pytest.mark.asyncio
async def test_non_members_and_users_without_active_group_cannot_list_maps() -> None:
    active_group_id = uuid4()
    service, _, _ = _service(None)

    with pytest.raises(ForbiddenException):
        await service.list_maps(
            query=MapListQuery(),
            group_id=active_group_id,
            credential=_credential(active_group_id),
            ctx=_ctx(),
        )

    no_group_service, _, _ = _service(GroupRole.MEMBER)
    with pytest.raises(ForbiddenException):
        await no_group_service.list_maps(
            query=MapListQuery(),
            group_id=None,
            credential=_credential(None),
            ctx=_ctx(),
        )


class ResultStub:
    def __init__(self, rows=None, total=None) -> None:
        self.rows = rows or []
        self.total = total

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def scalar_one(self):
        return self.total


class SessionStub:
    def __init__(self, rows=None) -> None:
        self.rows = rows or []
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return ResultStub(rows=self.rows, total=0)


@pytest.mark.asyncio
async def test_map_repository_filters_orders_and_paginates_with_group_scope() -> None:
    group_id = uuid4()
    query = MapListQuery(
        page=2,
        page_size=10,
        search="floor",
        status=MapStatus.ASSIGNED,
        tag_ids=[uuid4(), uuid4()],
        order_direction=MapOrderDirection.ASC,
    )
    session = SessionStub()

    rows, total = await MapRepository().list_maps(
        session=session, query=query, group_id=group_id, ctx=_ctx()
    )

    page_statement, count_statement = map(str, session.statements)
    assert rows == []
    assert total == 0
    assert "maps.group_id" in page_statement
    assert "maps.name" in page_statement and "LIKE" in page_statement
    assert "maps.status" in page_statement
    assert "map_tags" in page_statement
    assert "ORDER BY maps.created_at ASC, maps.id ASC" in page_statement
    assert "LIMIT" in page_statement and "OFFSET" in page_statement
    assert "count(distinct(maps.id))" in count_statement
    assert "ORDER BY" not in count_statement


@pytest.mark.asyncio
async def test_map_tags_repository_returns_group_scoped_tags_by_map() -> None:
    group_id = uuid4()
    map_id = uuid4()
    tag = Tag(
        id=uuid4(),
        group_id=group_id,
        name="Operations",
        description=None,
        color="#336699",
    )
    session = SessionStub(rows=[(map_id, tag)])

    result = await MapTagsRepository().get_by_map_ids(
        session=session, map_ids=[map_id], group_id=group_id, ctx=_ctx()
    )

    statement = str(session.statements[0])
    assert result == {map_id: [tag]}
    assert "JOIN maps" in statement
    assert "JOIN tags" in statement
    assert "map_tags.map_id" in statement
    assert "maps.group_id" in statement
    assert "tags.group_id" in statement


def test_router_declares_map_list_contract() -> None:
    router = MapRouter(
        SimpleNamespace(create_map=lambda: None, list_maps=lambda: None, save_boundary=lambda: None)
    ).router
    routes = {
        (route.path, tuple(sorted(route.methods))): route
        for route in router.routes
        if hasattr(route, "methods")
    }
    route = routes[("", ("GET",))]

    assert route.status_code == 200
    assert route.response_model.__name__ == "PaginationBaseResponse[MapListInfo]"
