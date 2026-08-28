from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.common.context import AppContext
from app.common.enum.context_actions import LIST_BOTS
from app.common.enum.robot import (
    RobotConnectionStatus,
    RobotModel,
    RobotOperationalStatus,
)
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import ForbiddenException
from app.common.schemas.bot import BotKeyValueInfo, BotListQuery
from app.common.schemas.group import GroupMemberInfo
from app.common.schemas.user import Credential
from app.repository.bot import BotRepository
from app.repository.robot_tags import RobotTagsRepository
from app.services.bot import BotService
from app.models.tag import Tag


def _ctx(actor_id=None) -> AppContext:
    return AppContext(trace_id=uuid4(), action=LIST_BOTS, actor=actor_id)


def _credential(active_group_id=None) -> Credential:
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
        return action == LIST_BOTS


class BotRepositoryStub:
    def __init__(self) -> None:
        self.bot_id = uuid4()

    async def list_bots(self, **kwargs):
        return [
            {
                "id": self.bot_id,
                "map_name": None,
                "name": "Scrubber 01",
                "serial_num": "SN-001",
                "model": RobotModel.PRO,
                "ip_address": "192.168.1.15",
                "operational_status": RobotOperationalStatus.IDLE,
                "created_at": datetime.now(timezone.utc),
                "connection_status": RobotConnectionStatus.ONLINE,
                "last_seen_at": None,
            }
        ], 1

    async def list_bot_key_value(self, **kwargs):
        self.key_value_kwargs = kwargs
        return [
            {"id": self.bot_id, "name": "Scrubber 01", "serial_num": "SN-001"}
        ]


class RobotTagsRepositoryStub:
    def __init__(self) -> None:
        self.tag_id = uuid4()

    async def get_by_robot_ids(self, *, robot_ids, group_id, **kwargs):
        now = datetime.now(timezone.utc)
        tag = Tag(
            id=self.tag_id,
            group_id=group_id,
            name="Operations",
            color="#336699",
            description="Operational robots",
        )
        tag.created_at = now
        tag.updated_at = now
        return {robot_id: [tag] for robot_id in robot_ids}


def _service(role: GroupRole | None) -> tuple[BotService, BotRepositoryStub]:
    bot_repository = BotRepositoryStub()
    robot_tags_repository = RobotTagsRepositoryStub()
    return BotService(
        repo=SimpleNamespace(
            bot_repo=lambda: bot_repository,
            robot_tags_repo=lambda: robot_tags_repository,
            transaction_wrapper=lambda callback: callback(SimpleNamespace()),
        ),
        permission_service=PermissionServiceStub(role),
    ), bot_repository


def test_bot_list_query_defaults_and_validation() -> None:
    group_id = uuid4()
    query = BotListQuery(group_id=group_id)

    assert query.group_id == group_id
    assert query.page == 1
    assert query.page_size == 10
    assert query.tag_ids is None

    with pytest.raises(ValidationError):
        BotListQuery(group_id=group_id, page_size=101)
    with pytest.raises(ValidationError, match="Tag identifiers must be unique"):
        BotListQuery(group_id=group_id, tag_ids=[uuid4()] * 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", list(GroupRole))
async def test_all_accepted_group_roles_can_list_bots(role: GroupRole) -> None:
    credential = _credential()
    group_id = uuid4()

    service, _ = _service(role)
    bots, total = await service.list_bots(
        query=BotListQuery(group_id=group_id),
        group_id=group_id,
        credential=credential,
        ctx=_ctx(credential.id),
    )

    assert total == 1
    assert bots[0].name == "Scrubber 01"
    assert bots[0].serial_num == "SN-001"
    assert bots[0].map_name is None
    assert [tag.name for tag in bots[0].tags] == ["Operations"]


@pytest.mark.asyncio
async def test_non_members_cannot_list_bots() -> None:
    credential = _credential()
    group_id = uuid4()

    with pytest.raises(ForbiddenException):
        service, _ = _service(None)
        await service.list_bots(
            query=BotListQuery(group_id=group_id),
            group_id=group_id,
            credential=credential,
            ctx=_ctx(credential.id),
        )


class ResultStub:
    def __init__(self, rows=None, total=None) -> None:
        self._rows = rows or []
        self._total = total

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def scalar_one(self):
        return self._total


class SessionStub:
    def __init__(self, rows=None) -> None:
        self.statements = []
        self.rows = rows or []

    async def execute(self, statement):
        self.statements.append(statement)
        return ResultStub(rows=self.rows, total=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", list(GroupRole))
async def test_all_accepted_group_roles_can_list_active_group_bot_key_values(
    role: GroupRole,
) -> None:
    active_group_id = uuid4()
    credential = _credential(active_group_id)
    service, bot_repository = _service(role)

    bots = await service.list_bot_key_value(
        search="scrub",
        group_id=credential.active_group_id,
        credential=credential,
        ctx=_ctx(credential.id),
    )

    assert bots == [
        BotKeyValueInfo(
            value=bot_repository.bot_id,
            label="Scrubber 01",
            serial_num="SN-001",
        )
    ]
    assert bot_repository.key_value_kwargs["group_id"] == active_group_id
    assert bot_repository.key_value_kwargs["search"] == "scrub"


@pytest.mark.asyncio
async def test_bot_key_values_require_active_group_and_membership() -> None:
    no_group_service, _ = _service(GroupRole.MEMBER)
    with pytest.raises(ForbiddenException, match="A group must be selected"):
        await no_group_service.list_bot_key_value(
            search=None,
            group_id=None,
            credential=_credential(),
            ctx=_ctx(),
        )

    group_id = uuid4()
    non_member_service, _ = _service(None)
    with pytest.raises(ForbiddenException):
        await non_member_service.list_bot_key_value(
            search=None,
            group_id=group_id,
            credential=_credential(group_id),
            ctx=_ctx(),
        )


@pytest.mark.asyncio
async def test_repository_lists_only_active_group_bot_key_values_with_search() -> None:
    bot_id = uuid4()
    session = SessionStub(
        rows=[{"id": bot_id, "name": "Scrubber 01", "serial_num": "SN-001"}]
    )

    rows = await BotRepository().list_bot_key_value(
        session=session,
        group_id=uuid4(),
        search="SN-001",
        ctx=_ctx(),
    )

    statement = str(session.statements[0])
    assert rows == [{"id": bot_id, "name": "Scrubber 01", "serial_num": "SN-001"}]
    assert "SELECT robots.id, robots.name, robots.serial_num" in statement
    assert "robots.group_id" in statement
    assert "robots.serial_num" in statement
    assert "ORDER BY robots.name ASC, robots.id ASC" in statement
    assert "LIMIT" not in statement and "OFFSET" not in statement


class RobotTagsSessionStub:
    def __init__(self, rows) -> None:
        self.rows = rows
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return ResultStub(rows=self.rows)


@pytest.mark.asyncio
async def test_repository_applies_list_filters_pagination_and_distinct_count() -> None:
    group_id = uuid4()
    query = BotListQuery(
        group_id=group_id,
        page=2,
        page_size=10,
        search="scrub",
        model=RobotModel.PRO,
        operational_status=RobotOperationalStatus.IDLE,
        connection_status=RobotConnectionStatus.ONLINE,
        tag_ids=[uuid4(), uuid4()],
    )
    session = SessionStub()

    rows, total = await BotRepository().list_bots(
        session=session, query=query, ctx=_ctx()
    )

    page_statement, count_statement = map(str, session.statements)
    assert rows == []
    assert total == 0
    assert "LEFT OUTER JOIN maps" in page_statement
    assert "robot_tags" in page_statement
    assert "robots.name" in page_statement
    assert "robots.serial_num" in page_statement
    assert "ORDER BY robots.created_at DESC, robots.id ASC" in page_statement
    assert "LIMIT" in page_statement and "OFFSET" in page_statement
    assert "count(distinct(robots.id))" in count_statement
    assert "ORDER BY" not in count_statement


@pytest.mark.asyncio
async def test_robot_tags_repository_returns_group_scoped_tags_by_robot() -> None:
    group_id = uuid4()
    robot_id = uuid4()
    tag = Tag(
        id=uuid4(),
        group_id=group_id,
        name="Operations",
        color="#336699",
        description=None,
    )
    session = RobotTagsSessionStub(rows=[(robot_id, tag)])

    result = await RobotTagsRepository().get_by_robot_ids(
        session=session,
        robot_ids=[robot_id],
        group_id=group_id,
        ctx=_ctx(),
    )

    statement = str(session.statements[0])
    assert result == {robot_id: [tag]}
    assert "JOIN robots" in statement
    assert "JOIN tags" in statement
    assert "robot_tags.robot_id" in statement
    assert "robots.group_id" in statement
    assert "tags.group_id" in statement


@pytest.mark.asyncio
async def test_robot_tags_repository_skips_query_for_no_bots() -> None:
    session = RobotTagsSessionStub(rows=[])

    result = await RobotTagsRepository().get_by_robot_ids(
        session=session,
        robot_ids=[],
        group_id=uuid4(),
        ctx=_ctx(),
    )

    assert result == {}
    assert session.statements == []
