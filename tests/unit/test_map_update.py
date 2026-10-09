from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.common.context import AppContext
from app.common.enum.context_actions import UPDATE_MAP
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import BadRequestException, NotFoundException
from app.common.schemas.group import GroupMemberInfo
from app.common.schemas.map import MapUpdateDTO
from app.common.schemas.user import Credential
from app.handler.map import MapHandler
from app.router.map import MapRouter
from app.services.map import MapService


def _credential(group_id: UUID) -> Credential:
    return Credential(
        id=uuid4(), email="admin@example.com", status=UserStatus.ACTIVE,
        active_group_id=group_id,
    )


def _context(actor_id: UUID) -> AppContext:
    return AppContext(trace_id=uuid4(), action=UPDATE_MAP, actor=actor_id)


class PermissionServiceStub:
    async def get_group_member(self, credential, ctx, group_id=None):
        now = datetime.now(timezone.utc)
        return GroupMemberInfo(
            member_id=credential.id,
            group_id=group_id,
            role=GroupRole.ADMIN,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def is_action_executable(role, action, is_owner=False):
        return action == UPDATE_MAP and role in {GroupRole.ADMIN, GroupRole.OWNER}


class MapRepositoryStub:
    def __init__(self, map_record):
        self.map_record = map_record
        self.other_names = {}
        self.locked = False

    async def get_by_id_and_group_for_update(self, *, map_id, group_id, **kwargs):
        self.locked = True
        if self.map_record.id != map_id or self.map_record.group_id != group_id:
            return None
        return self.map_record

    async def get_by_group_and_name(self, *, group_id, name, **kwargs):
        return self.other_names.get((group_id, name))

    async def update_metadata(
        self, *, map_record, name, description, update_description, **kwargs
    ):
        changed = map_record.name != name
        map_record.name = name
        if update_description and map_record.description != description:
            map_record.description = description
            changed = True
        return changed


class TagRepositoryStub:
    def __init__(self, known_tags):
        self.known_tags = set(known_tags)

    async def get_by_ids_and_group(self, *, tag_ids, **kwargs):
        return [tag_id for tag_id in tag_ids if tag_id in self.known_tags]


class MapTagsRepositoryStub:
    def __init__(self, existing_tags):
        self.tags = set(existing_tags)

    async def synchronize_for_map(self, *, tag_ids, **kwargs):
        requested = set(tag_ids)
        changed = requested != self.tags
        self.tags = requested
        return changed


def _service(
    group_id, *, tag_ids=(), existing_tag_ids=(), map_record=None, other_names=None
):
    record = map_record or SimpleNamespace(
        id=uuid4(), group_id=group_id, name="Original", description="Description"
    )
    map_repository = MapRepositoryStub(record)
    map_repository.other_names = other_names or {}
    map_tags_repository = MapTagsRepositoryStub(existing_tag_ids)
    async def transaction(callback):
        async def flush():
            return None
        return await callback(SimpleNamespace(flush=flush))
    registry = SimpleNamespace(
        map_repo=lambda: map_repository,
        tag_repo=lambda: TagRepositoryStub(tag_ids),
        map_tags_repo=lambda: map_tags_repository,
        transaction_wrapper=transaction,
    )
    return MapService(registry, PermissionServiceStub()), record, map_repository, map_tags_repository


def test_update_schema_requires_nonempty_name_and_unique_tags():
    with pytest.raises(ValidationError):
        MapUpdateDTO(description="Only description")
    with pytest.raises(ValidationError):
        MapUpdateDTO(name=" ")
    tag_id = uuid4()
    with pytest.raises(ValidationError, match="Tag identifiers must be unique"):
        MapUpdateDTO(name="Floor", tags=[tag_id, tag_id])


@pytest.mark.asyncio
async def test_update_ignores_null_or_omitted_and_saves_empty_description():
    group_id = uuid4()
    actor = _credential(group_id)
    service, record, _, _ = _service(group_id)
    await service.update_map(
        map_id=record.id,
        map_update=MapUpdateDTO(name="Renamed", description=""),
        group_id=group_id,
        credential=actor,
        ctx=_context(actor.id),
    )
    assert record.name == "Renamed"
    assert record.description == ""

    await service.update_map(
        map_id=record.id,
        map_update=MapUpdateDTO(name="Renamed", description=None, tags=None),
        group_id=group_id,
        credential=actor,
        ctx=_context(actor.id),
    )
    assert record.description == ""


@pytest.mark.asyncio
async def test_update_syncs_tags_and_rejects_missing_group_tag():
    group_id, tag_id, removed_tag_id = uuid4(), uuid4(), uuid4()
    actor = _credential(group_id)
    service, record, _, map_tags = _service(
        group_id,
        tag_ids=[tag_id, removed_tag_id],
        existing_tag_ids=[removed_tag_id],
    )
    await service.update_map(
        map_id=record.id,
        map_update=MapUpdateDTO(name="Original", tags=[tag_id]),
        group_id=group_id,
        credential=actor,
        ctx=_context(actor.id),
    )
    assert map_tags.tags == {tag_id}

    await service.update_map(
        map_id=record.id,
        map_update=MapUpdateDTO(name="Original", tags=[]),
        group_id=group_id,
        credential=actor,
        ctx=_context(actor.id),
    )
    assert map_tags.tags == set()

    with pytest.raises(NotFoundException, match="tags were not found"):
        await service.update_map(
            map_id=record.id,
            map_update=MapUpdateDTO(name="Original", tags=[uuid4()]),
            group_id=group_id,
            credential=actor,
            ctx=_context(actor.id),
        )


@pytest.mark.asyncio
async def test_update_requires_map_in_active_group_and_rejects_duplicate_name():
    group_id = uuid4()
    actor = _credential(group_id)
    service, record, map_repository, _ = _service(group_id)
    missing_service, _, _, _ = _service(group_id)
    with pytest.raises(NotFoundException, match="Map not found"):
        await missing_service.update_map(
            map_id=uuid4(), map_update=MapUpdateDTO(name="New"),
            group_id=group_id, credential=actor, ctx=_context(actor.id),
        )
    other_map = SimpleNamespace(id=uuid4())
    map_repository.other_names[(group_id, "Taken")] = other_map
    with pytest.raises(BadRequestException, match="already exists"):
        await service.update_map(
            map_id=record.id, map_update=MapUpdateDTO(name="Taken"),
            group_id=group_id, credential=actor, ctx=_context(actor.id),
        )


@pytest.mark.asyncio
async def test_handler_returns_success_and_router_declares_put_contract():
    class ServiceStub:
        async def update_map(self, **kwargs):
            return None

    handler = MapHandler(ServiceStub())
    result = await handler.update_map(
        map_id=uuid4(), map_update=MapUpdateDTO(name="Floor"),
        credential=_credential(uuid4()),
    )
    assert result == "Success"

    router = MapRouter(handler).router
    route = next(
        route for route in router.routes
        if getattr(route, "path", None) == "/{map_id}" and "PUT" in route.methods
    )
    assert route.status_code == 200
    assert route.response_model is str
