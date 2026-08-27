from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.common.context import AppContext
from app.common.enum.context_actions import CREATE_MAP
from app.common.enum.map import MapStatus
from app.common.enum.robot import (
    RobotConnectionStatus,
    RobotModel,
    RobotOperationalStatus,
)
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import BadRequestException, ForbiddenException, NotFoundException
from app.common.schemas.group import GroupMemberInfo
from app.common.schemas.map import MapCreateDTO
from app.common.schemas.user import Credential
from app.models.map import Map
from app.models.robot import Robot
from app.models.tag import Tag
from app.repository.bot import BotRepository
from app.repository.map import MapRepository
from app.router.map import MapRouter
from app.services.map import MapService


def _credential() -> Credential:
    return Credential(id=uuid4(), email="admin@example.com", status=UserStatus.ACTIVE)


def _ctx(actor_id: UUID | None = None) -> AppContext:
    return AppContext(trace_id=uuid4(), action=CREATE_MAP, actor=actor_id)


def _request(group_id: UUID, **overrides) -> MapCreateDTO:
    payload = {
        "group_id": group_id,
        "name": "Floor 1",
        "description": "Main floor",
        "dimension_x": "20",
        "dimension_y": "30",
    }
    payload.update(overrides)
    return MapCreateDTO(**payload)


def _tag(group_id: UUID) -> Tag:
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
    return tag


def _robot(group_id: UUID, map_id: UUID | None = None) -> Robot:
    robot = Robot(
        id=uuid4(),
        group_id=group_id,
        map_id=map_id,
        name="Scrubber 01",
        serial_num="SN-001",
        model=RobotModel.STANDARD,
        ip_address=None,
        connection_status=RobotConnectionStatus.OFFLINE,
        operational_status=RobotOperationalStatus.IDLE,
    )
    return robot


class PermissionServiceStub:
    def __init__(self, role: GroupRole | None) -> None:
        self.role = role

    async def get_group_member(self, credential, ctx, group_id=None):
        if self.role is None:
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
        return action == CREATE_MAP and role in {GroupRole.OWNER, GroupRole.ADMIN}


class MapRepositoryStub:
    def __init__(self) -> None:
        self.maps: list[Map] = []

    async def get_by_group_and_name(self, *, group_id, name, **kwargs):
        return next(
            (item for item in self.maps if item.group_id == group_id and item.name == name),
            None,
        )

    async def create_map(self, *, group_id, name, description, dimension_x, dimension_y, status, tags, **kwargs):
        now = datetime.now(timezone.utc)
        map_record = Map(
            id=uuid4(),
            group_id=group_id,
            name=name,
            description=description,
            dimension_x=dimension_x,
            dimension_y=dimension_y,
            status=status,
        )
        map_record.tags = list(tags)
        map_record.created_at = now
        map_record.updated_at = now
        self.maps.append(map_record)
        return map_record


class BotRepositoryStub:
    def __init__(self, robots: list[Robot]) -> None:
        self.robots = robots

    async def get_by_ids_and_group_for_update(self, *, bot_ids, group_id, **kwargs):
        return [
            robot
            for robot in self.robots
            if robot.id in bot_ids and robot.group_id == group_id
        ]

    async def assign_map(self, *, robots, map_id, **kwargs):
        for robot in robots:
            robot.map_id = map_id


class TagRepositoryStub:
    def __init__(self, tags: list[Tag]) -> None:
        self.tags = tags

    async def get_by_ids_and_group(self, *, tag_ids, group_id, **kwargs):
        return [
            tag for tag in self.tags if tag.id in tag_ids and tag.group_id == group_id
        ]


def _service(role: GroupRole | None, robots: list[Robot], tags: list[Tag]):
    map_repository = MapRepositoryStub()
    registry = SimpleNamespace(
        map_repo=lambda: map_repository,
        bot_repo=lambda: BotRepositoryStub(robots),
        tag_repo=lambda: TagRepositoryStub(tags),
        transaction_wrapper=lambda callback: callback(SimpleNamespace()),
    )
    return MapService(registry, PermissionServiceStub(role)), map_repository


def test_map_create_schema_validates_input() -> None:
    group_id = uuid4()
    robot_id = uuid4()
    tag_id = uuid4()
    payload = _request(
        group_id,
        name="  Floor 1  ",
        robot_ids=[robot_id],
        tags=[tag_id],
    )

    assert payload.name == "Floor 1"
    assert payload.robot_ids == [robot_id]
    assert payload.tags == [tag_id]

    for overrides in [
        {"name": ""},
        {"dimension_x": ""},
        {"dimension_x": "twenty"},
        {"dimension_y": "NaN"},
        {"dimension_y": "Infinity"},
        {"robot_ids": [robot_id, robot_id]},
        {"tags": [tag_id, tag_id]},
        {"unexpected": "field"},
    ]:
        with pytest.raises(ValidationError):
            _request(group_id, **overrides)

    numeric_dimensions = _request(group_id, dimension_x="20.5", dimension_y="1e3")
    assert numeric_dimensions.dimension_x == "20.5"
    assert numeric_dimensions.dimension_y == "1e3"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [GroupRole.OWNER, GroupRole.ADMIN])
async def test_owner_and_admin_create_map_with_tags_and_robots(role: GroupRole) -> None:
    group_id = uuid4()
    robot = _robot(group_id)
    tag = _tag(group_id)
    service, _ = _service(role, [robot], [tag])
    credential = _credential()

    result = await service.create_map(
        map_create=_request(group_id, robot_ids=[robot.id], tags=[tag.id]),
        group_id=group_id,
        credential=credential,
        ctx=_ctx(credential.id),
    )

    assert result.status == MapStatus.ASSIGNED
    assert result.robot_ids == [robot.id]
    assert [item.id for item in result.tags] == [tag.id]
    assert robot.map_id == result.id


@pytest.mark.asyncio
async def test_map_without_robots_is_unassigned() -> None:
    group_id = uuid4()
    service, _ = _service(GroupRole.ADMIN, [], [])
    credential = _credential()

    result = await service.create_map(
        map_create=_request(group_id),
        group_id=group_id,
        credential=credential,
        ctx=_ctx(credential.id),
    )

    assert result.status == MapStatus.UNASSIGNED
    assert result.robot_ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role", [GroupRole.MODERATOR, GroupRole.MEMBER, GroupRole.GUEST, None]
)
async def test_unprivileged_callers_cannot_create_maps(role) -> None:
    group_id = uuid4()
    service, _ = _service(role, [], [])

    with pytest.raises(ForbiddenException):
        await service.create_map(
            map_create=_request(group_id),
            group_id=group_id,
            credential=_credential(),
            ctx=_ctx(),
        )


@pytest.mark.asyncio
async def test_existing_map_name_and_invalid_assignments_are_rejected() -> None:
    group_id = uuid4()
    robot = _robot(group_id, map_id=uuid4())
    service, repository = _service(GroupRole.ADMIN, [robot], [])
    credential = _credential()

    with pytest.raises(BadRequestException, match="already assigned"):
        await service.create_map(
            map_create=_request(group_id, robot_ids=[robot.id]),
            group_id=group_id,
            credential=credential,
            ctx=_ctx(credential.id),
        )
    assert repository.maps == []

    with pytest.raises(NotFoundException, match="robots were not found"):
        await service.create_map(
            map_create=_request(group_id, robot_ids=[uuid4()]),
            group_id=group_id,
            credential=credential,
            ctx=_ctx(credential.id),
        )


@pytest.mark.asyncio
async def test_map_name_is_unique_per_group_and_tags_must_belong_to_group() -> None:
    group_id = uuid4()
    credential = _credential()
    service, _ = _service(GroupRole.ADMIN, [], [])
    request = _request(group_id)

    await service.create_map(
        map_create=request,
        group_id=group_id,
        credential=credential,
        ctx=_ctx(credential.id),
    )
    with pytest.raises(BadRequestException, match="already exists"):
        await service.create_map(
            map_create=request,
            group_id=group_id,
            credential=credential,
            ctx=_ctx(credential.id),
        )

    other_group_id = uuid4()
    result = await service.create_map(
        map_create=_request(other_group_id),
        group_id=other_group_id,
        credential=credential,
        ctx=_ctx(credential.id),
    )
    assert result.group_id == other_group_id

    other_group_tag = _tag(other_group_id)
    other_service, _ = _service(GroupRole.ADMIN, [], [other_group_tag])
    requested_group_id = uuid4()
    with pytest.raises(NotFoundException, match="tags were not found"):
        await other_service.create_map(
            map_create=_request(requested_group_id, tags=[other_group_tag.id]),
            group_id=requested_group_id,
            credential=credential,
            ctx=_ctx(credential.id),
        )


class ResultStub:
    def scalar_one_or_none(self):
        return None

    def scalars(self):
        return self

    def all(self):
        return []


class SessionStub:
    def __init__(self) -> None:
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return ResultStub()


@pytest.mark.asyncio
async def test_repositories_scope_map_name_and_lock_robot_assignments() -> None:
    session = SessionStub()
    group_id = uuid4()
    await MapRepository().get_by_group_and_name(
        session=session, group_id=group_id, name="Floor 1", ctx=_ctx()
    )
    await BotRepository().get_by_ids_and_group_for_update(
        session=session, bot_ids=[uuid4()], group_id=group_id, ctx=_ctx()
    )

    map_statement, robot_statement = map(str, session.statements)
    assert "maps.group_id" in map_statement
    assert "maps.name" in map_statement
    assert "robots.id" in robot_statement
    assert "robots.group_id" in robot_statement
    assert "FOR UPDATE" in robot_statement


def test_router_declares_create_map_route() -> None:
    router = MapRouter(
        SimpleNamespace(create_map=lambda: None, list_maps=lambda: None)
    ).router
    routes = {
        (route.path, tuple(sorted(route.methods))): route
        for route in router.routes
        if hasattr(route, "methods")
    }
    route = routes[("", ("POST",))]

    assert route.status_code == 201
    assert route.response_model.__name__ == "BaseResponse[MapInfo]"
