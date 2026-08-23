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
from app.common.schemas.bot import BotListQuery
from app.common.schemas.group import GroupMemberInfo
from app.common.schemas.user import Credential
from app.repository.bot import BotRepository
from app.services.bot import BotService


def _ctx(actor_id=None) -> AppContext:
    return AppContext(trace_id=uuid4(), action=LIST_BOTS, actor=actor_id)


def _credential() -> Credential:
    return Credential(id=uuid4(), email="member@example.com", status=UserStatus.ACTIVE)


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
    async def list_bots(self, **kwargs):
        return [
            {
                "map_name": None,
                "serial_num": "SN-001",
                "model": RobotModel.PRO,
                "ip_address": "192.168.1.15",
                "operational_status": RobotOperationalStatus.IDLE,
                "created_at": datetime.now(timezone.utc),
                "connection_status": RobotConnectionStatus.ONLINE,
            }
        ], 1


def _service(role: GroupRole | None) -> BotService:
    return BotService(
        repo=SimpleNamespace(
            bot_repo=lambda: BotRepositoryStub(),
            transaction_wrapper=lambda callback: callback(SimpleNamespace()),
        ),
        permission_service=PermissionServiceStub(role),
    )


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

    bots, total = await _service(role).list_bots(
        query=BotListQuery(group_id=group_id),
        group_id=group_id,
        credential=credential,
        ctx=_ctx(credential.id),
    )

    assert total == 1
    assert bots[0].serial_num == "SN-001"
    assert bots[0].map_name is None


@pytest.mark.asyncio
async def test_non_members_cannot_list_bots() -> None:
    credential = _credential()
    group_id = uuid4()

    with pytest.raises(ForbiddenException):
        await _service(None).list_bots(
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
    def __init__(self) -> None:
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return ResultStub(total=0)


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
