from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.common.context import AppContext
from app.common.enum.context_actions import DELETE_BOT
from app.common.enum.robot import (
    RobotConnectionStatus,
    RobotModel,
    RobotOperationalStatus,
)
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
)
from app.common.schemas.group import GroupMemberInfo
from app.common.schemas.user import Credential
from app.models.robot import Robot
from app.repository.bot import BotRepository
from app.router.bot import BotRouter
from app.services.bot import BotService


def _credential(active_group_id: UUID | None) -> Credential:
    return Credential(
        id=uuid4(),
        email="operator@example.com",
        status=UserStatus.ACTIVE,
        active_group_id=active_group_id,
    )


def _ctx(actor_id: UUID) -> AppContext:
    return AppContext(trace_id=uuid4(), action=DELETE_BOT, actor=actor_id)


def _bot(group_id: UUID, status: RobotOperationalStatus) -> Robot:
    now = datetime.now(timezone.utc)
    bot = Robot(
        id=uuid4(),
        group_id=group_id,
        map_id=None,
        name="Scrubber 01",
        serial_num="SN-001",
        model=RobotModel.PRO,
        ip_address=None,
        connection_status=RobotConnectionStatus.OFFLINE,
        operational_status=status,
    )
    bot.created_at = now
    bot.updated_at = now
    return bot


class PermissionServiceStub:
    def __init__(self, role: GroupRole | None) -> None:
        self.role = role

    async def get_group_member(self, credential, ctx, group_id=None):
        if self.role is None or credential.active_group_id is None:
            return None
        now = datetime.now(timezone.utc)
        return GroupMemberInfo(
            member_id=credential.id,
            group_id=group_id or credential.active_group_id,
            role=self.role,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def is_action_executable(role, action, is_owner=False):
        return action == DELETE_BOT and role in {
            GroupRole.OWNER,
            GroupRole.ADMIN,
            GroupRole.MODERATOR,
        }


class BotRepositoryStub:
    def __init__(self, bots: list[Robot]) -> None:
        self.bots = {(bot.id, bot.group_id): bot for bot in bots}
        self.locked_lookup: tuple[UUID, UUID] | None = None
        self.deleted: tuple[UUID, UUID] | None = None

    async def get_by_id_and_group_for_update(self, *, bot_id, group_id, **kwargs):
        self.locked_lookup = (bot_id, group_id)
        return self.bots.get((bot_id, group_id))

    async def hard_delete_bot(self, *, bot_id, group_id, **kwargs):
        self.deleted = (bot_id, group_id)
        return self.bots.pop((bot_id, group_id), None)


def _service(role: GroupRole | None, bots: list[Robot]):
    bot_repository = BotRepositoryStub(bots)
    registry = SimpleNamespace(
        bot_repo=lambda: bot_repository,
        transaction_wrapper=lambda callback: callback(SimpleNamespace()),
    )
    return BotService(registry, PermissionServiceStub(role)), bot_repository


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role", [GroupRole.OWNER, GroupRole.ADMIN, GroupRole.MODERATOR]
)
async def test_privileged_roles_hard_delete_bot_in_active_group(
    role: GroupRole,
) -> None:
    group_id = uuid4()
    bot = _bot(group_id, RobotOperationalStatus.IDLE)
    service, repository = _service(role, [bot])
    credential = _credential(group_id)

    await service.delete_bot(
        bot_id=bot.id, credential=credential, ctx=_ctx(credential.id)
    )

    assert repository.locked_lookup == (bot.id, group_id)
    assert repository.deleted == (bot.id, group_id)
    assert repository.bots == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [GroupRole.MEMBER, GroupRole.GUEST, None])
async def test_unprivileged_or_non_member_callers_cannot_delete_bot(role) -> None:
    group_id = uuid4()
    bot = _bot(group_id, RobotOperationalStatus.IDLE)
    service, repository = _service(role, [bot])
    credential = _credential(group_id)

    with pytest.raises(ForbiddenException):
        await service.delete_bot(
            bot_id=bot.id, credential=credential, ctx=_ctx(credential.id)
        )

    assert repository.bots == {(bot.id, group_id): bot}


@pytest.mark.asyncio
async def test_callers_without_an_active_group_cannot_delete_bot() -> None:
    group_id = uuid4()
    bot = _bot(group_id, RobotOperationalStatus.IDLE)
    service, repository = _service(GroupRole.ADMIN, [bot])
    credential = _credential(None)

    with pytest.raises(ForbiddenException, match="group must be selected"):
        await service.delete_bot(
            bot_id=bot.id, credential=credential, ctx=_ctx(credential.id)
        )

    assert repository.bots == {(bot.id, group_id): bot}


@pytest.mark.asyncio
async def test_unknown_or_out_of_group_bot_returns_not_found() -> None:
    active_group_id = uuid4()
    other_group_id = uuid4()
    other_group_bot = _bot(other_group_id, RobotOperationalStatus.IDLE)
    service, repository = _service(GroupRole.ADMIN, [other_group_bot])
    credential = _credential(active_group_id)

    with pytest.raises(NotFoundException, match="Bot not found"):
        await service.delete_bot(
            bot_id=other_group_bot.id,
            credential=credential,
            ctx=_ctx(credential.id),
        )
    with pytest.raises(NotFoundException, match="Bot not found"):
        await service.delete_bot(
            bot_id=uuid4(), credential=credential, ctx=_ctx(credential.id)
        )

    assert repository.deleted is None
    assert repository.bots == {(other_group_bot.id, other_group_id): other_group_bot}


@pytest.mark.asyncio
async def test_executing_bot_cannot_be_deleted() -> None:
    group_id = uuid4()
    bot = _bot(group_id, RobotOperationalStatus.EXECUTING)
    service, repository = _service(GroupRole.ADMIN, [bot])
    credential = _credential(group_id)

    with pytest.raises(BadRequestException, match="stop the operation or wait"):
        await service.delete_bot(
            bot_id=bot.id, credential=credential, ctx=_ctx(credential.id)
        )

    assert repository.deleted is None
    assert repository.bots == {(bot.id, group_id): bot}


class ResultStub:
    def scalar_one_or_none(self):
        return None


class SessionStub:
    def __init__(self) -> None:
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return ResultStub()


@pytest.mark.asyncio
async def test_repository_scopes_locked_lookup_and_hard_delete_to_group() -> None:
    session = SessionStub()
    bot_id = uuid4()
    group_id = uuid4()
    repository = BotRepository()

    await repository.get_by_id_and_group_for_update(
        session=session, bot_id=bot_id, group_id=group_id, ctx=_ctx(uuid4())
    )
    await repository.hard_delete_bot(
        session=session, bot_id=bot_id, group_id=group_id, ctx=_ctx(uuid4())
    )

    lookup_statement, delete_statement = map(str, session.statements)
    assert "FROM robots" in lookup_statement
    assert "robots.id" in lookup_statement
    assert "robots.group_id" in lookup_statement
    assert "FOR UPDATE" in lookup_statement
    assert "DELETE FROM robots" in delete_statement
    assert "robots.id" in delete_statement
    assert "robots.group_id" in delete_statement
    assert "RETURNING robots.id" in delete_statement


def test_router_registers_delete_bot_contract() -> None:
    class HandlerStub:
        async def list_bots(self): ...
        async def create_bot(self): ...
        async def delete_bot(self): ...

    router = BotRouter(handler=HandlerStub()).router
    routes = {
        (route.path, tuple(sorted(route.methods))): route
        for route in router.routes
        if hasattr(route, "methods")
    }
    delete_route = routes[("/{bot_id}", ("DELETE",))]
    assert delete_route.status_code == 204
