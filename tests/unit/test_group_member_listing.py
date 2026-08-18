from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.common.context import AppContext
from app.common.enum.context_actions import LIST_GROUP_MEMBERS
from app.common.enum.group_member_status import GroupMemberInvitationStatus
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import ForbiddenException
from app.common.schemas.group import (
    GroupMemberListQuery,
    GroupMemberOrderBy,
    GroupMemberOrderDirection,
)
from app.common.schemas.user import Credential
from app.repository.group_members import GroupMembersRepository
from app.services.group import GroupService


def _ctx(actor_id):
    return AppContext(trace_id=uuid4(), action=LIST_GROUP_MEMBERS, actor=actor_id)


class PermissionServiceStub:
    def __init__(self, role: GroupRole) -> None:
        self.role = role

    async def get_group_member(self, credential, ctx, group_id=None):
        now = datetime.now(timezone.utc)
        return SimpleNamespace(
            member_id=credential.id,
            group_id=group_id,
            role=self.role,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def is_action_executable(role, action, is_owner=False):
        return role in {GroupRole.OWNER, GroupRole.ADMIN} and action == LIST_GROUP_MEMBERS


class GroupMembersRepositoryStub:
    def __init__(self, rows, total) -> None:
        self.rows = rows
        self.total = total
        self.query = None

    async def list_group_members(self, *, query, **kwargs):
        self.query = query
        return self.rows, self.total


def _service(role: GroupRole, rows=None, total=0):
    group_id = uuid4()
    member_repo = GroupMembersRepositoryStub(rows or [], total)
    repo = SimpleNamespace(
        group_repo=lambda: SimpleNamespace(
            get_group=lambda **kwargs: _async_value(SimpleNamespace(id=group_id))
        ),
        group_members_repo=lambda: member_repo,
        transaction_wrapper=lambda callback: callback(SimpleNamespace()),
    )
    return GroupService(
        repo=repo,
        permission_service=PermissionServiceStub(role),
        add_group_member_topic=SimpleNamespace(),
    ), group_id, member_repo


async def _async_value(value):
    return value


def test_group_member_list_query_defaults_and_validation() -> None:
    query = GroupMemberListQuery(group_id=uuid4())

    assert query.page == 1
    assert query.page_size == 10
    assert query.order_by == GroupMemberOrderBy.JOINED_DATE
    assert query.order_direction == GroupMemberOrderDirection.DESC

    with pytest.raises(ValueError):
        GroupMemberListQuery(group_id=uuid4(), page_size=101)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [GroupRole.OWNER, GroupRole.ADMIN])
async def test_owner_and_admin_can_list_group_members_with_membership_data(
    role: GroupRole,
) -> None:
    member_id = uuid4()
    joined_at = datetime.now(timezone.utc)
    membership = SimpleNamespace(
        member_id=member_id,
        created_at=joined_at,
        role=GroupRole.MEMBER,
        invitation_status=GroupMemberInvitationStatus.ACCEPTED,
    )
    user = SimpleNamespace(
        image_url="https://example.com/avatar.png",
        email="alex@example.com",
        name="Alex",
        status=UserStatus.ACTIVE,
    )
    service, group_id, member_repo = _service(
        role, rows=[(membership, user)], total=1
    )
    credential = Credential(
        id=uuid4(), email="admin@example.com", status=UserStatus.ACTIVE
    )
    query = GroupMemberListQuery(group_id=group_id, email="alex")

    result = await service.list_group_members(
        query=query,
        group_id=group_id,
        credential=credential,
        ctx=_ctx(credential.id),
    )

    assert member_repo.query == query
    members, total = result
    assert total == 1
    assert members[0].member_id == member_id
    assert members[0].joined_at == joined_at
    assert members[0].status == UserStatus.ACTIVE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role", [GroupRole.MODERATOR, GroupRole.MEMBER, GroupRole.GUEST]
)
async def test_lower_roles_cannot_list_group_members(role: GroupRole) -> None:
    service, group_id, _ = _service(role)
    credential = Credential(
        id=uuid4(), email="member@example.com", status=UserStatus.ACTIVE
    )

    with pytest.raises(ForbiddenException):
        await service.list_group_members(
            query=GroupMemberListQuery(group_id=group_id),
            group_id=group_id,
            credential=credential,
            ctx=_ctx(credential.id),
        )


class ResultStub:
    def __init__(self, rows=None, total=None) -> None:
        self.rows = rows or []
        self.total = total

    def all(self):
        return self.rows

    def scalar_one(self):
        return self.total


class SessionStub:
    def __init__(self) -> None:
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return ResultStub(total=0)


@pytest.mark.asyncio
async def test_repository_applies_filters_order_and_pagination() -> None:
    group_id = uuid4()
    query = GroupMemberListQuery(
        group_id=group_id,
        page=2,
        page_size=10,
        order_by=GroupMemberOrderBy.NAME,
        order_direction=GroupMemberOrderDirection.ASC,
        email="alex",
        role=GroupRole.MEMBER,
        status=UserStatus.ACTIVE,
    )
    session = SessionStub()
    repository = GroupMembersRepository(redis_client=None)

    rows, total = await repository.list_group_members(
        session=session, query=query, ctx=_ctx(uuid4())
    )

    page_statement, count_statement = map(str, session.statements)
    assert rows == []
    assert total == 0
    assert "users.email" in page_statement
    assert "group_members.role" in page_statement
    assert "users.status" in page_statement
    assert "ORDER BY users.name ASC" in page_statement
    assert "LIMIT" in page_statement and "OFFSET" in page_statement
    assert "ORDER BY" not in count_statement
