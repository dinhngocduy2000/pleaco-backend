from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.common.context import AppContext
from app.common.enum.context_actions import LIST_TAGS
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import ForbiddenException
from app.common.schemas.group import GroupMemberInfo
from app.common.schemas.user import Credential
from app.models.tag import Tag
from app.repository.tag import TagRepository
from app.router.tag import TagRouter
from app.services.tag import TagService


def _ctx(actor_id: UUID | None = None) -> AppContext:
    return AppContext(trace_id=uuid4(), action=LIST_TAGS, actor=actor_id)


def _credential() -> Credential:
    return Credential(id=uuid4(), email="member@example.com", status=UserStatus.ACTIVE)


def _tag(group_id: UUID, name: str, color: str) -> Tag:
    now = datetime.now(timezone.utc)
    tag = Tag(id=uuid4(), group_id=group_id, name=name, color=color, description="Hidden")
    tag.created_at = now
    tag.updated_at = now
    return tag


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
        return action == LIST_TAGS


class TagRepositoryStub:
    def __init__(self, tags: list[Tag]) -> None:
        self.tags = tags

    async def list_by_group(self, *, group_id, **kwargs):
        return [tag for tag in self.tags if tag.group_id == group_id]


def _service(role: GroupRole | None, tags: list[Tag]) -> TagService:
    repository = TagRepositoryStub(tags)
    registry = SimpleNamespace(
        tag_repo=lambda: repository,
        transaction_wrapper=lambda callback: callback(SimpleNamespace()),
    )
    return TagService(registry, PermissionServiceStub(role))


@pytest.mark.asyncio
@pytest.mark.parametrize("role", list(GroupRole))
async def test_all_accepted_group_roles_can_list_tags(role: GroupRole) -> None:
    group_id = uuid4()
    other_group_id = uuid4()
    tags = [_tag(group_id, "Alpha", "#111111"), _tag(other_group_id, "Beta", "#222222")]
    credential = _credential()

    result = await _service(role, tags).list_tags(
        group_id=group_id,
        credential=credential,
        ctx=_ctx(credential.id),
    )

    assert [tag.name for tag in result] == ["Alpha"]
    assert result[0].model_dump() == {
        "id": tags[0].id,
        "name": "Alpha",
        "color": "#111111",
    }


@pytest.mark.asyncio
async def test_non_members_cannot_list_tags() -> None:
    credential = _credential()

    with pytest.raises(ForbiddenException):
        await _service(None, []).list_tags(
            group_id=uuid4(), credential=credential, ctx=_ctx(credential.id)
        )


class ResultStub:
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
async def test_repository_filters_and_orders_tags_by_group() -> None:
    session = SessionStub()

    tags = await TagRepository().list_by_group(
        session=session, group_id=uuid4(), ctx=_ctx()
    )

    assert tags == []
    statement = str(session.statements[0])
    assert "WHERE tags.group_id" in statement
    assert "ORDER BY tags.name ASC, tags.id ASC" in statement


def test_tag_router_declares_unpaginated_list_response() -> None:
    handler = SimpleNamespace(list_tags=lambda: None)
    router = TagRouter(handler).router
    route = next(route for route in router.routes if route.path == "")

    assert route.methods == {"GET"}
    assert route.response_model.__name__ == "BaseResponse[list[TagListInfo]]"
