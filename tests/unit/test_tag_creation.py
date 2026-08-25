from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.common.context import AppContext
from app.common.enum.context_actions import CREATE_TAG
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import BadRequestException, ForbiddenException
from app.common.schemas.group import GroupMemberInfo
from app.common.schemas.tags import TagCreateDTO
from app.common.schemas.user import Credential
from app.models.tag import Tag
from app.repository.tag import TagRepository
from app.router.tag import TagRouter
from app.services.tag import TagService


def _ctx(actor_id: UUID | None = None) -> AppContext:
    return AppContext(trace_id=uuid4(), action=CREATE_TAG, actor=actor_id)


def _credential() -> Credential:
    return Credential(id=uuid4(), email="admin@example.com", status=UserStatus.ACTIVE)


def _request(group_id: UUID, **overrides) -> TagCreateDTO:
    payload = {
        "group_id": group_id,
        "name": "Operations",
        "description": "Operational robots",
        "color": "#336699",
    }
    payload.update(overrides)
    return TagCreateDTO(**payload)


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
        return action == CREATE_TAG


class TagRepositoryStub:
    def __init__(self, tags: list[Tag]) -> None:
        self.tags = tags

    async def get_by_group_and_name(self, *, group_id, name, **kwargs):
        return next(
            (
                tag
                for tag in self.tags
                if tag.group_id == group_id and tag.name == name
            ),
            None,
        )

    async def create_tag(self, *, group_id, name, description, color, **kwargs):
        now = datetime.now(timezone.utc)
        tag = Tag(
            id=uuid4(),
            group_id=group_id,
            name=name,
            description=description,
            color=color,
        )
        tag.created_at = now
        tag.updated_at = now
        self.tags.append(tag)
        return tag


def _service(role: GroupRole | None, tags: list[Tag]) -> TagService:
    tag_repository = TagRepositoryStub(tags)
    registry = SimpleNamespace(
        tag_repo=lambda: tag_repository,
        transaction_wrapper=lambda callback: callback(SimpleNamespace()),
    )
    return TagService(registry, PermissionServiceStub(role))


def test_tag_create_dto_validates_and_normalizes_input() -> None:
    group_id = uuid4()
    tag = _request(
        group_id,
        name="  Operations  ",
        description="  Operational robots  ",
        color="#A1b2C3",
    )

    assert tag.name == "Operations"
    assert tag.description == "Operational robots"
    assert tag.color == "#A1b2C3"

    invalid_payloads = [
        {"name": ""},
        {"name": "x" * 51},
        {"description": "x" * 256},
        {"color": "336699"},
        {"color": "#ABC"},
        {"unexpected": "field"},
    ]
    for overrides in invalid_payloads:
        with pytest.raises(ValidationError):
            _request(group_id, **overrides)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [GroupRole.OWNER, GroupRole.ADMIN])
async def test_owner_and_admin_can_create_tags(role: GroupRole) -> None:
    group_id = uuid4()
    credential = _credential()

    tag = await _service(role, []).create_tag(
        tag_create=_request(group_id),
        group_id=group_id,
        credential=credential,
        ctx=_ctx(credential.id),
    )

    assert tag.name == "Operations"
    assert tag.color == "#336699"
    assert tag.description == "Operational robots"
    assert tag.id is not None
    assert tag.created_at is not None
    assert tag.updated_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role", [GroupRole.MODERATOR, GroupRole.MEMBER, GroupRole.GUEST, None]
)
async def test_unprivileged_or_non_member_callers_cannot_create_tags(role) -> None:
    group_id = uuid4()

    with pytest.raises(ForbiddenException):
        await _service(role, []).create_tag(
            tag_create=_request(group_id),
            group_id=group_id,
            credential=_credential(),
            ctx=_ctx(),
        )


@pytest.mark.asyncio
async def test_tag_name_is_unique_within_its_group_only() -> None:
    first_group_id = uuid4()
    second_group_id = uuid4()
    credential = _credential()
    service = _service(GroupRole.ADMIN, [])

    await service.create_tag(
        tag_create=_request(first_group_id),
        group_id=first_group_id,
        credential=credential,
        ctx=_ctx(credential.id),
    )
    with pytest.raises(BadRequestException, match="already exists"):
        await service.create_tag(
            tag_create=_request(first_group_id),
            group_id=first_group_id,
            credential=credential,
            ctx=_ctx(credential.id),
        )

    tag = await service.create_tag(
        tag_create=_request(second_group_id),
        group_id=second_group_id,
        credential=credential,
        ctx=_ctx(credential.id),
    )
    assert tag.name == "Operations"


class ResultStub:
    def scalar_one_or_none(self):
        return None


class SessionStub:
    def __init__(self) -> None:
        self.statements = []
        self.added = []
        self.flushed = False

    async def execute(self, statement):
        self.statements.append(statement)
        return ResultStub()

    def add(self, model):
        self.added.append(model)

    async def flush(self):
        self.flushed = True


@pytest.mark.asyncio
async def test_repository_scopes_duplicate_lookup_and_creates_tag() -> None:
    session = SessionStub()
    group_id = uuid4()
    repository = TagRepository()

    existing = await repository.get_by_group_and_name(
        session=session, group_id=group_id, name="Operations", ctx=_ctx()
    )
    created = await repository.create_tag(
        session=session,
        group_id=group_id,
        name="Operations",
        description=None,
        color="#336699",
        ctx=_ctx(),
    )

    assert existing is None
    assert "WHERE tags.group_id" in str(session.statements[0])
    assert "tags.name" in str(session.statements[0])
    assert created.group_id == group_id
    assert session.added == [created]
    assert session.flushed is True


def test_tag_router_declares_create_route() -> None:
    handler = SimpleNamespace(list_tags=lambda: None, create_tag=lambda: None)
    router = TagRouter(handler).router
    routes = {(route.path, tuple(sorted(route.methods))): route for route in router.routes}
    route = routes[("", ("POST",))]

    assert route.status_code == 201
    assert route.response_model.__name__ == "BaseResponse[TagInfo]"
