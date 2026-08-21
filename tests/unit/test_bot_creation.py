from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.common.context import AppContext
from app.common.enum.context_actions import CREATE_BOT
from app.common.enum.robot import (
    RobotConnectionStatus,
    RobotModel,
    RobotOperationalStatus,
)
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import BadRequestException, ForbiddenException, NotFoundException
from app.common.schemas.bot import BotCreateDTO
from app.common.schemas.group import GroupMemberInfo
from app.common.schemas.user import Credential
from app.models.robot import Robot
from app.models.tag import Tag
from app.services.bot import BotService


class BotRepositoryStub:
    def __init__(self, tags: list[Tag]) -> None:
        self.tags = {tag.id: tag for tag in tags}
        self.bots: dict[tuple[UUID, str], Robot] = {}

    async def get_by_group_and_serial(self, *, group_id, serial_num, **kwargs):
        return self.bots.get((group_id, serial_num))

    async def get_tags_by_ids(self, *, tag_ids, **kwargs):
        return [self.tags[tag_id] for tag_id in tag_ids if tag_id in self.tags]

    async def create_bot(self, *, bot_create, tags, **kwargs):
        now = datetime.now(timezone.utc)
        bot = Robot(
            id=uuid4(),
            group_id=bot_create.group_id,
            map_id=bot_create.map_id,
            name=bot_create.name,
            serial_num=bot_create.serial_num,
            model=bot_create.model,
            ip_address=bot_create.ip_address,
            connection_status=bot_create.connection_status,
            operational_status=bot_create.operational_status,
        )
        bot.tags = list(tags)
        bot.created_at = now
        bot.updated_at = now
        self.bots[(bot.group_id, bot.serial_num)] = bot
        return bot


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
        return action == CREATE_BOT and role in {
            GroupRole.OWNER,
            GroupRole.ADMIN,
            GroupRole.MODERATOR,
        }


def _tag() -> Tag:
    now = datetime.now(timezone.utc)
    tag = Tag(id=uuid4(), name="Operations", color="#336699", description=None)
    tag.created_at = now
    tag.updated_at = now
    return tag


def _credential() -> Credential:
    return Credential(id=uuid4(), email="operator@example.com", status=UserStatus.ACTIVE)


def _request(group_id: UUID, tag_ids: list[UUID], **overrides) -> BotCreateDTO:
    return BotCreateDTO(
        group_id=group_id,
        name="Scrubber 01",
        serial_num="SN-001",
        model=RobotModel.STANDARD,
        map_id=uuid4(),
        ip_address="192.168.1.15",
        tags=tag_ids,
        **overrides,
    )


def _service(role: GroupRole | None, tags: list[Tag]) -> tuple[BotService, BotRepositoryStub]:
    bot_repository = BotRepositoryStub(tags)
    registry = SimpleNamespace(
        bot_repo=lambda: bot_repository,
        transaction_wrapper=lambda callback: callback(SimpleNamespace()),
    )
    return BotService(registry, PermissionServiceStub(role)), bot_repository


def _ctx() -> AppContext:
    return AppContext(trace_id=uuid4(), action=CREATE_BOT)


def test_bot_create_schema_rejects_duplicate_tags_and_unknown_fields() -> None:
    group_id = uuid4()
    tag_id = uuid4()
    with pytest.raises(ValidationError, match="Tag identifiers must be unique"):
        _request(group_id, [tag_id, tag_id])
    with pytest.raises(ValidationError):
        _request(group_id, [], unexpected="value")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role", [GroupRole.OWNER, GroupRole.ADMIN, GroupRole.MODERATOR]
)
async def test_authorized_roles_create_bot_with_provisioning_defaults(role: GroupRole) -> None:
    tag = _tag()
    service, _ = _service(role, [tag])
    group_id = uuid4()

    result = await service.create_bot(
        bot_create=_request(group_id, [tag.id]),
        group_id=group_id,
        credential=_credential(),
        ctx=_ctx(),
    )

    assert result.group_id == group_id
    assert result.map_id is None
    assert result.connection_status == RobotConnectionStatus.OFFLINE
    assert result.operational_status == RobotOperationalStatus.IDLE
    assert [item.id for item in result.tags] == [tag.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [GroupRole.MEMBER, GroupRole.GUEST, None])
async def test_unprivileged_or_non_member_callers_cannot_create_bots(role) -> None:
    tag = _tag()
    service, _ = _service(role, [tag])
    group_id = uuid4()

    with pytest.raises(ForbiddenException):
        await service.create_bot(
            bot_create=_request(group_id, [tag.id]),
            group_id=group_id,
            credential=_credential(),
            ctx=_ctx(),
        )


@pytest.mark.asyncio
async def test_serial_number_is_unique_within_a_group_only() -> None:
    tag = _tag()
    service, _ = _service(GroupRole.ADMIN, [tag])
    credential = _credential()
    first_group_id = uuid4()

    await service.create_bot(
        bot_create=_request(first_group_id, [tag.id]),
        group_id=first_group_id,
        credential=credential,
        ctx=_ctx(),
    )
    with pytest.raises(BadRequestException, match="serial number already exists"):
        await service.create_bot(
            bot_create=_request(first_group_id, [tag.id]),
            group_id=first_group_id,
            credential=credential,
            ctx=_ctx(),
        )

    second_group_id = uuid4()
    result = await service.create_bot(
        bot_create=_request(second_group_id, [tag.id]),
        group_id=second_group_id,
        credential=credential,
        ctx=_ctx(),
    )
    assert result.group_id == second_group_id


@pytest.mark.asyncio
async def test_missing_tag_rejects_bot_creation() -> None:
    service, repository = _service(GroupRole.ADMIN, [])
    group_id = uuid4()

    with pytest.raises(NotFoundException, match="tags were not found"):
        await service.create_bot(
            bot_create=_request(group_id, [uuid4()]),
            group_id=group_id,
            credential=_credential(),
            ctx=_ctx(),
        )
    assert repository.bots == {}
