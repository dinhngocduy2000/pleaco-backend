from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.common.context import AppContext
from app.common.enum.context_actions import LIST_GROUP_MEMBERS
from app.common.enum.group_member_status import GroupMemberInvitationStatus
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import BadRequestException, ForbiddenException
from app.common.schemas.common import (
    CursorPaginationMetadata,
    CursorPaginationResponse,
    CursorPayload,
)
from app.common.schemas.group import GroupMemberListInfo, GroupMemberListQuery
from app.common.schemas.user import Credential
from app.common.utils.cursor_pagination import decode_cursor, encode_cursor
from app.handler.group import GroupHandler
from app.repository.group_members import GroupMembersRepository
from app.router.group import GroupRouter
from app.services.group import GroupService


def _ctx(actor_id):
    return AppContext(trace_id=uuid4(), action=LIST_GROUP_MEMBERS, actor=actor_id)


def _credential():
    return Credential(
        id=uuid4(), email="admin@example.com", status=UserStatus.ACTIVE
    )


def _row(created_at: datetime | None = None):
    membership = SimpleNamespace(
        member_id=uuid4(),
        created_at=created_at or datetime.now(timezone.utc),
        role=GroupRole.MEMBER,
        invitation_status=GroupMemberInvitationStatus.ACCEPTED,
    )
    user = SimpleNamespace(
        image_url="https://example.com/avatar.png",
        email=f"{membership.member_id}@example.com",
        name="Alex",
        status=UserStatus.ACTIVE,
    )
    return membership, user


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
    def __init__(self, rows, has_more) -> None:
        self.rows = rows
        self.has_more = has_more
        self.query = None
        self.cursor = None

    async def list_group_members(self, *, query, cursor, **kwargs):
        self.query = query
        self.cursor = cursor
        return self.rows, self.has_more


def _service(role: GroupRole, rows=None, has_more=False):
    group_id = uuid4()
    member_repo = GroupMembersRepositoryStub(rows or [], has_more)
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

    assert query.after is None
    assert query.before is None
    assert query.limit == 10

    with pytest.raises(ValidationError):
        GroupMemberListQuery(group_id=uuid4(), limit=101)
    with pytest.raises(ValidationError):
        GroupMemberListQuery(group_id=uuid4(), after="a", before="b")


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [GroupRole.OWNER, GroupRole.ADMIN])
async def test_first_page_returns_members_and_next_cursor(role: GroupRole) -> None:
    row = _row()
    service, group_id, member_repo = _service(role, rows=[row], has_more=True)
    credential = _credential()
    query = GroupMemberListQuery(group_id=group_id, email="alex")

    members, pagination = await service.list_group_members(
        query=query,
        group_id=group_id,
        credential=credential,
        ctx=_ctx(credential.id),
    )

    assert member_repo.query == query
    assert member_repo.cursor is None
    assert members[0].member_id == row[0].member_id
    assert pagination.has_next is True
    assert pagination.has_previous is False
    assert pagination.next_cursor is not None
    assert pagination.previous_cursor is None
    assert decode_cursor(pagination.next_cursor).id == row[0].member_id


@pytest.mark.asyncio
async def test_after_page_returns_bidirectional_metadata() -> None:
    cursor_payload = CursorPayload(
        created_at=datetime.now(timezone.utc), id=uuid4()
    )
    row = _row(cursor_payload.created_at - timedelta(seconds=1))
    service, group_id, member_repo = _service(
        GroupRole.ADMIN, rows=[row], has_more=False
    )
    query = GroupMemberListQuery(
        group_id=group_id,
        after=encode_cursor(cursor_payload),
    )
    credential = _credential()

    _, pagination = await service.list_group_members(
        query=query,
        group_id=group_id,
        credential=credential,
        ctx=_ctx(credential.id),
    )

    assert member_repo.cursor == cursor_payload
    assert pagination.has_previous is True
    assert pagination.previous_cursor is not None
    assert pagination.has_next is False
    assert pagination.next_cursor is None


@pytest.mark.asyncio
async def test_before_page_returns_bidirectional_metadata() -> None:
    cursor_payload = CursorPayload(
        created_at=datetime.now(timezone.utc), id=uuid4()
    )
    row = _row(cursor_payload.created_at + timedelta(seconds=1))
    service, group_id, member_repo = _service(
        GroupRole.ADMIN, rows=[row], has_more=False
    )
    query = GroupMemberListQuery(
        group_id=group_id,
        before=encode_cursor(cursor_payload),
    )
    credential = _credential()

    _, pagination = await service.list_group_members(
        query=query,
        group_id=group_id,
        credential=credential,
        ctx=_ctx(credential.id),
    )

    assert member_repo.cursor == cursor_payload
    assert pagination.has_previous is False
    assert pagination.previous_cursor is None
    assert pagination.has_next is True
    assert pagination.next_cursor is not None


@pytest.mark.asyncio
async def test_empty_page_has_no_navigation_metadata() -> None:
    service, group_id, _ = _service(GroupRole.ADMIN)
    credential = _credential()
    cursor = encode_cursor(
        CursorPayload(created_at=datetime.now(timezone.utc), id=uuid4())
    )

    members, pagination = await service.list_group_members(
        query=GroupMemberListQuery(group_id=group_id, after=cursor),
        group_id=group_id,
        credential=credential,
        ctx=_ctx(credential.id),
    )

    assert members == []
    assert pagination.has_next is False
    assert pagination.has_previous is False
    assert pagination.next_cursor is None
    assert pagination.previous_cursor is None


@pytest.mark.asyncio
async def test_invalid_cursor_is_a_bad_request() -> None:
    service, group_id, _ = _service(GroupRole.ADMIN)
    credential = _credential()

    with pytest.raises(BadRequestException, match="Invalid pagination cursor"):
        await service.list_group_members(
            query=GroupMemberListQuery(group_id=group_id, after="invalid!"),
            group_id=group_id,
            credential=credential,
            ctx=_ctx(credential.id),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role", [GroupRole.MODERATOR, GroupRole.MEMBER, GroupRole.GUEST]
)
async def test_lower_roles_cannot_list_group_members(role: GroupRole) -> None:
    service, group_id, _ = _service(role)
    credential = _credential()

    with pytest.raises(ForbiddenException):
        await service.list_group_members(
            query=GroupMemberListQuery(group_id=group_id),
            group_id=group_id,
            credential=credential,
            ctx=_ctx(credential.id),
        )


class ResultStub:
    def __init__(self, rows=None) -> None:
        self.rows = rows or []

    def all(self):
        return self.rows


class SessionStub:
    def __init__(self, rows=None) -> None:
        self.rows = rows or []
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return ResultStub(rows=self.rows)


@pytest.mark.asyncio
async def test_repository_applies_filters_and_forward_keyset_pagination() -> None:
    cursor = CursorPayload(
        created_at=datetime.now(timezone.utc),
        id=uuid4(),
    )
    query = GroupMemberListQuery(
        group_id=uuid4(),
        after=encode_cursor(cursor),
        limit=10,
        email="alex",
        role=GroupRole.MEMBER,
        status=UserStatus.ACTIVE,
    )
    session = SessionStub()

    rows, has_more = await GroupMembersRepository(None).list_group_members(
        session=session,
        query=query,
        cursor=cursor,
        ctx=_ctx(uuid4()),
    )

    statement = str(session.statements[0])
    assert rows == []
    assert has_more is False
    assert len(session.statements) == 1
    assert "users.email" in statement
    assert "group_members.role" in statement
    assert "users.status" in statement
    assert "(group_members.created_at, group_members.member_id) <" in statement
    assert "ORDER BY group_members.created_at DESC, group_members.member_id DESC" in statement
    assert "LIMIT" in statement
    assert "OFFSET" not in statement


@pytest.mark.asyncio
async def test_repository_truncates_then_reverses_previous_page() -> None:
    now = datetime.now(timezone.utc)
    closest = _row(now + timedelta(seconds=1))
    middle = _row(now + timedelta(seconds=2))
    furthest = _row(now + timedelta(seconds=3))
    extra = _row(now + timedelta(seconds=4))
    cursor = CursorPayload(created_at=now, id=uuid4())
    query = GroupMemberListQuery(
        group_id=uuid4(), before=encode_cursor(cursor), limit=3
    )
    session = SessionStub(rows=[closest, middle, furthest, extra])

    rows, has_more = await GroupMembersRepository(None).list_group_members(
        session=session,
        query=query,
        cursor=cursor,
        ctx=_ctx(uuid4()),
    )

    statement = str(session.statements[0])
    assert has_more is True
    assert rows == [furthest, middle, closest]
    assert "(group_members.created_at, group_members.member_id) >" in statement
    assert "ORDER BY group_members.created_at ASC, group_members.member_id ASC" in statement


@pytest.mark.asyncio
async def test_handler_builds_cursor_response() -> None:
    row = _row()
    member = GroupMemberListInfo(
        member_id=row[0].member_id,
        image_url=row[1].image_url,
        email=row[1].email,
        name=row[1].name,
        joined_at=row[0].created_at,
        role=row[0].role,
        status=row[1].status,
        invitation_status=row[0].invitation_status,
    )

    class ServiceStub:
        async def list_group_members(self, **kwargs):
            return [member], CursorPaginationMetadata(
                limit=10,
                next_cursor=None,
                previous_cursor=None,
                has_next=False,
                has_previous=False,
            )

    handler = GroupHandler(ServiceStub(), SimpleNamespace())
    query = GroupMemberListQuery(group_id=uuid4())

    response = await handler.list_group_members(query=query, credential=_credential())

    assert isinstance(response, CursorPaginationResponse)
    assert response.items == [member]
    assert response.limit == 10


def test_router_uses_cursor_response_and_documentation() -> None:
    class HandlerStub:
        async def create_group(self): ...
        async def list_group_members(self): ...
        async def invite_group_members(self): ...
        async def update_group_member(self): ...
        async def delete_group_member(self): ...
        async def validate_group_invitation(self): ...
        async def get_group_invitation(self): ...
        async def list_group_key_value(self): ...
        async def get_group(self): ...
        async def switch_current_user_group(self): ...

    router = GroupRouter(handler=HandlerStub()).router
    route = next(route for route in router.routes if route.path == "/members")

    assert route.response_model == CursorPaginationResponse[GroupMemberListInfo]
    assert "after" in route.description
    assert "page_size" not in route.description
    assert "order_by" not in route.description
