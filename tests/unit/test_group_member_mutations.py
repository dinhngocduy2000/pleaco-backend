from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.common.context import AppContext
from app.common.enum.context_actions import EDIT_MEMBER, REMOVE_MEMBER
from app.common.enum.group_member_status import GroupMemberInvitationStatus
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import ForbiddenException, NotFoundException
from app.common.schemas.group import GroupMemberInfo, GroupMemberUpdate
from app.common.schemas.user import Credential
from app.models.group_members import GroupMembers
from app.router.group import GroupRouter
from app.services.group import GroupService


def _ctx(action: str, actor: UUID) -> AppContext:
    return AppContext(trace_id=uuid4(), action=action, actor=actor)


class GroupMembersRepositoryStub:
    def __init__(self, members: list[GroupMembers]) -> None:
        self.members = {(member.member_id, member.group_id): member for member in members}
        self.cached_updates: list[GroupMembers] = []
        self.cached_deletes: list[tuple[UUID, UUID]] = []

    async def get_group_member_by_id(self, *, member_id, group_id, accepted_only=False, **kwargs):
        member = self.members.get((member_id, group_id))
        if member is None:
            return None
        if accepted_only and member.invitation_status != GroupMemberInvitationStatus.ACCEPTED:
            return None
        return member

    async def update_group_member_role(self, *, member_id, group_id, role, **kwargs):
        member = self.members.get((member_id, group_id))
        if member is None or member.invitation_status != GroupMemberInvitationStatus.ACCEPTED:
            return None
        member.role = role
        member.updated_at = datetime.now(timezone.utc)
        return member

    async def hard_delete_group_member(self, *, member_id, group_id, **kwargs):
        return self.members.pop((member_id, group_id), None)

    async def set_group_member_redis(self, *, member, **kwargs):
        self.cached_updates.append(member)

    async def delete_group_member_redis(self, *, group_id, member_id, **kwargs):
        self.cached_deletes.append((member_id, group_id))


class PermissionServiceStub:
    def __init__(self, role: GroupRole) -> None:
        self.role = role

    async def get_group_member(self, credential, ctx, group_id=None):
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
        return role in {GroupRole.OWNER, GroupRole.ADMIN} and action in {
            EDIT_MEMBER,
            REMOVE_MEMBER,
        }


class InvitationRepositoryStub:
    def __init__(self, invitations=None) -> None:
        self.invitations = invitations or {}
        self.consumed = []

    async def get(self, invitation_id, ctx):
        return self.invitations.get(invitation_id)

    async def consume(self, invitation, ctx):
        self.consumed.append(invitation)


def _member(
    member_id: UUID,
    group_id: UUID,
    role: GroupRole,
    status: GroupMemberInvitationStatus = GroupMemberInvitationStatus.ACCEPTED,
    invitation_id: UUID | None = None,
) -> GroupMembers:
    now = datetime.now(timezone.utc)
    member = GroupMembers(
        member_id=member_id,
        group_id=group_id,
        role=role,
        invitation_status=status,
        invitation_id=invitation_id,
    )
    member.created_at = now
    member.updated_at = now
    return member


def _service(
    requester_role: GroupRole,
    target_role: GroupRole = GroupRole.MEMBER,
    target_status: GroupMemberInvitationStatus = GroupMemberInvitationStatus.ACCEPTED,
):
    group_id = uuid4()
    requester_id = uuid4()
    target_id = uuid4()
    invitation_id = uuid4() if target_status == GroupMemberInvitationStatus.PENDING else None
    target = _member(
        target_id, group_id, target_role, target_status, invitation_id
    )
    members_repo = GroupMembersRepositoryStub([target])
    invitation = SimpleNamespace(invitation_id=invitation_id) if invitation_id else None
    invitation_repo = InvitationRepositoryStub(
        {invitation_id: invitation} if invitation_id else None
    )
    repo = SimpleNamespace(
        group_repo=lambda: SimpleNamespace(
            get_group=lambda **kwargs: _async_value(SimpleNamespace(id=group_id))
        ),
        group_members_repo=lambda: members_repo,
        group_invitation_repo=lambda: invitation_repo,
        transaction_wrapper=lambda callback: callback(SimpleNamespace()),
    )
    return (
        GroupService(repo, PermissionServiceStub(requester_role), SimpleNamespace()),
        group_id,
        requester_id,
        target_id,
        members_repo,
        invitation_repo,
    )


async def _async_value(value):
    return value


def _credential(user_id: UUID) -> Credential:
    return Credential(id=user_id, email="admin@example.com", status=UserStatus.ACTIVE)


def test_group_member_update_schema_forbids_unrelated_fields() -> None:
    assert GroupMemberUpdate(role=GroupRole.MEMBER).role == GroupRole.MEMBER
    with pytest.raises(ValidationError):
        GroupMemberUpdate(role=GroupRole.MEMBER, email="new@example.com")


@pytest.mark.asyncio
async def test_owner_can_update_lower_member_role_and_refreshes_cache() -> None:
    service, group_id, requester_id, target_id, members_repo, _ = _service(GroupRole.OWNER)

    result = await service.update_group_member(
        group_id=group_id,
        member_id=target_id,
        member_update=GroupMemberUpdate(role=GroupRole.ADMIN),
        credential=_credential(requester_id),
        ctx=_ctx(EDIT_MEMBER, requester_id),
    )

    assert result.role == GroupRole.ADMIN
    assert members_repo.cached_updates == [members_repo.members[(target_id, group_id)]]


@pytest.mark.asyncio
@pytest.mark.parametrize("requester_role", [GroupRole.MODERATOR, GroupRole.MEMBER, GroupRole.GUEST])
async def test_lower_roles_cannot_update_or_delete_members(requester_role: GroupRole) -> None:
    service, group_id, requester_id, target_id, _, _ = _service(requester_role)

    with pytest.raises(ForbiddenException):
        await service.update_group_member(
            group_id=group_id,
            member_id=target_id,
            member_update=GroupMemberUpdate(role=GroupRole.GUEST),
            credential=_credential(requester_id),
            ctx=_ctx(EDIT_MEMBER, requester_id),
        )
    with pytest.raises(ForbiddenException):
        await service.delete_group_member(
            group_id=group_id,
            member_id=target_id,
            credential=_credential(requester_id),
            ctx=_ctx(REMOVE_MEMBER, requester_id),
        )


@pytest.mark.asyncio
async def test_member_mutations_reject_self_equal_or_higher_targets_and_role_escalation() -> None:
    service, group_id, requester_id, _, _, _ = _service(GroupRole.ADMIN)

    with pytest.raises(ForbiddenException, match="own group membership"):
        await service.update_group_member(
            group_id=group_id,
            member_id=requester_id,
            member_update=GroupMemberUpdate(role=GroupRole.MEMBER),
            credential=_credential(requester_id),
            ctx=_ctx(EDIT_MEMBER, requester_id),
        )
    with pytest.raises(ForbiddenException, match="own group membership"):
        await service.delete_group_member(
            group_id=group_id,
            member_id=requester_id,
            credential=_credential(requester_id),
            ctx=_ctx(REMOVE_MEMBER, requester_id),
        )

    service, group_id, requester_id, target_id, _, _ = _service(
        GroupRole.ADMIN, target_role=GroupRole.ADMIN
    )
    with pytest.raises(ForbiddenException, match="equal or higher"):
        await service.delete_group_member(
            group_id=group_id,
            member_id=target_id,
            credential=_credential(requester_id),
            ctx=_ctx(REMOVE_MEMBER, requester_id),
        )
    service, group_id, requester_id, target_id, _, _ = _service(GroupRole.ADMIN)
    with pytest.raises(ForbiddenException, match="role lower"):
        await service.update_group_member(
            group_id=group_id,
            member_id=target_id,
            member_update=GroupMemberUpdate(role=GroupRole.ADMIN),
            credential=_credential(requester_id),
            ctx=_ctx(EDIT_MEMBER, requester_id),
        )


@pytest.mark.asyncio
async def test_update_requires_accepted_membership_and_delete_revokes_pending_invitation() -> None:
    service, group_id, requester_id, target_id, members_repo, invitation_repo = _service(
        GroupRole.ADMIN, target_status=GroupMemberInvitationStatus.PENDING
    )
    with pytest.raises(NotFoundException, match="Group member not found"):
        await service.update_group_member(
            group_id=group_id,
            member_id=target_id,
            member_update=GroupMemberUpdate(role=GroupRole.GUEST),
            credential=_credential(requester_id),
            ctx=_ctx(EDIT_MEMBER, requester_id),
        )

    await service.delete_group_member(
        group_id=group_id,
        member_id=target_id,
        credential=_credential(requester_id),
        ctx=_ctx(REMOVE_MEMBER, requester_id),
    )
    assert (target_id, group_id) not in members_repo.members
    assert invitation_repo.consumed
    assert members_repo.cached_deletes == [(target_id, group_id)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        GroupMemberInvitationStatus.ACCEPTED,
        GroupMemberInvitationStatus.REJECTED,
    ],
)
async def test_delete_hard_deletes_accepted_and_rejected_memberships(
    status: GroupMemberInvitationStatus,
) -> None:
    service, group_id, requester_id, target_id, members_repo, invitation_repo = _service(
        GroupRole.ADMIN, target_status=status
    )

    await service.delete_group_member(
        group_id=group_id,
        member_id=target_id,
        credential=_credential(requester_id),
        ctx=_ctx(REMOVE_MEMBER, requester_id),
    )

    assert (target_id, group_id) not in members_repo.members
    assert invitation_repo.consumed == []


@pytest.mark.asyncio
async def test_member_mutations_reject_missing_group_and_membership() -> None:
    service, group_id, requester_id, _, _, _ = _service(GroupRole.ADMIN)
    service.repo.group_repo = lambda: SimpleNamespace(
        get_group=lambda **kwargs: _async_value(None)
    )
    with pytest.raises(NotFoundException, match="Group not found"):
        await service.update_group_member(
            group_id=group_id,
            member_id=uuid4(),
            member_update=GroupMemberUpdate(role=GroupRole.GUEST),
            credential=_credential(requester_id),
            ctx=_ctx(EDIT_MEMBER, requester_id),
        )

    service, group_id, requester_id, _, _, _ = _service(GroupRole.ADMIN)
    with pytest.raises(NotFoundException, match="Group member not found"):
        await service.delete_group_member(
            group_id=group_id,
            member_id=uuid4(),
            credential=_credential(requester_id),
            ctx=_ctx(REMOVE_MEMBER, requester_id),
        )


def test_router_registers_group_member_mutation_contracts() -> None:
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
    routes = {
        (route.path, tuple(sorted(route.methods))): route
        for route in router.routes
        if hasattr(route, "methods")
    }
    put_route = routes[("/{group_id}/members/{member_id}", ("PUT",))]
    delete_route = routes[("/{group_id}/members/{member_id}", ("DELETE",))]
    assert put_route.status_code == 200
    assert delete_route.status_code == 204
