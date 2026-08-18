from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.common.context import AppContext
from app.common.enum.group_member_status import GroupMemberInvitationStatus
from app.common.enum.context_actions import INVITE_MEMBER
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import BadRequestException, ForbiddenException, NotFoundException
from app.common.schemas.group import GroupInvitationInfo, GroupMemberCreate, GroupMemberInfo
from app.common.schemas.mail import SendMailResponse
from app.common.schemas.user import Credential
from app.external.queues.queue import TopicMessage
from app.external.queues.topics.add_group_member import AddGroupMemberTopic
from app.models.group import Group
from app.models.group_members import GroupMembers
from app.models.user import User
from app.services.group import GroupService


def _ctx() -> AppContext:
    return AppContext(trace_id=uuid4(), action=INVITE_MEMBER, actor=uuid4())


class GroupMembersRepositoryStub:
    def __init__(self, existing_member_ids: set | None = None) -> None:
        self.existing_member_ids = existing_member_ids or set()
        self.created = []
        self.members = {}

    async def list_existing_member_ids(self, **kwargs):
        return self.existing_member_ids | {
            member.member_id
            for member in self.members.values()
            if member.invitation_status == GroupMemberInvitationStatus.ACCEPTED
        }

    async def create(self, *, group_members, **kwargs):
        self.created.extend(group_members)
        for member in group_members:
            self.members[(member.member_id, member.group_id)] = member
        return group_members

    async def list_group_members_by_ids(self, *, member_ids, **kwargs):
        return {
            member_id: self.members[(member_id, kwargs["group_id"])]
            for member_id in member_ids
            if (member_id, kwargs["group_id"]) in self.members
        }

    async def upsert_pending_invitations(
        self, *, invitations, existing_members, **kwargs
    ):
        pending_members = []
        for invitation in invitations:
            member = existing_members.get(invitation.member_id)
            if member is None:
                member = GroupMembers(
                    member_id=invitation.member_id,
                    group_id=invitation.group_id,
                )
                self.created.append(member)
                self.members[(member.member_id, member.group_id)] = member
            member.role = invitation.role
            member.invitation_status = GroupMemberInvitationStatus.PENDING
            member.invitation_id = invitation.invitation_id
            member.invitation_expires_at = invitation.expires_at
            pending_members.append(member)
        return pending_members

    async def get_group_member_by_id(
        self, *, member_id, group_id, accepted_only=False, **kwargs
    ):
        member = self.members.get((member_id, group_id))
        if member is not None and (
            not accepted_only
            or member.invitation_status == GroupMemberInvitationStatus.ACCEPTED
        ):
            return member
        if member_id in self.existing_member_ids:
            return SimpleNamespace(
                member_id=member_id,
                group_id=group_id,
                role=GroupRole.MEMBER,
                invitation_status=GroupMemberInvitationStatus.ACCEPTED,
            )
        return None

    async def accept_pending_invitation(
        self, *, invitation_id, group_id, member_id, now, **kwargs
    ):
        member = self.members.get((member_id, group_id))
        if (
            member is None
            or member.invitation_id != invitation_id
            or member.invitation_status != GroupMemberInvitationStatus.PENDING
            or member.invitation_expires_at <= now
        ):
            return None
        member.invitation_status = GroupMemberInvitationStatus.ACCEPTED
        member.invitation_id = None
        member.invitation_expires_at = None
        return member

    async def reject_expired_invitations(self, *, now, **kwargs):
        rejected = 0
        for member in self.members.values():
            if (
                member.invitation_status == GroupMemberInvitationStatus.PENDING
                and member.invitation_expires_at <= now
            ):
                member.invitation_status = GroupMemberInvitationStatus.REJECTED
                member.invitation_id = None
                member.invitation_expires_at = None
                rejected += 1
        return rejected

    async def set_group_member_redis(self, **kwargs):
        return None


class GroupInvitationRepositoryStub:
    def __init__(self) -> None:
        self.saved = []
        self.invitations = {}
        self.pending = {}

    async def save(self, invitation, ctx):
        self.saved.append(invitation)

    async def replace_pending_invitation(self, invitation, ctx):
        pending_key = (invitation.group_id, invitation.member_id)
        old_invitation_id = self.pending.get(pending_key)
        if old_invitation_id is not None:
            self.invitations.pop(old_invitation_id, None)
        self.invitations[invitation.invitation_id] = invitation
        self.pending[pending_key] = invitation.invitation_id
        self.saved.append(invitation)

    async def get(self, invitation_id, ctx):
        return self.invitations.get(invitation_id)

    async def delete(self, invitation_id, ctx):
        self.invitations.pop(invitation_id, None)

    async def consume(self, invitation, ctx):
        self.invitations.pop(invitation.invitation_id, None)
        pending_key = (invitation.group_id, invitation.member_id)
        if self.pending.get(pending_key) == invitation.invitation_id:
            self.pending.pop(pending_key)


class TopicStub:
    def __init__(self) -> None:
        self.messages = []

    async def publish_invitation(self, message):
        self.messages.append(message)
        return "message-id"


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
        return role == GroupRole.ADMIN and action == INVITE_MEMBER


def _service(
    users: list[User], existing_member_ids: set | None = None
) -> tuple[GroupService, GroupMembersRepositoryStub, GroupInvitationRepositoryStub, TopicStub]:
    group_id = uuid4()
    group = Group(id=group_id, name="Weekend plans", owner_id=uuid4())
    members_repo = GroupMembersRepositoryStub(existing_member_ids)
    invitation_repo = GroupInvitationRepositoryStub()
    topic = TopicStub()
    repo = SimpleNamespace(
        group_repo=lambda: SimpleNamespace(
            get_group=lambda **kwargs: _async_value(group)
        ),
        user_repo=lambda: SimpleNamespace(
            get_by_emails=lambda **kwargs: _async_value(users)
        ),
        group_members_repo=lambda: members_repo,
        group_invitation_repo=lambda: invitation_repo,
        transaction_wrapper=lambda callback: callback(SimpleNamespace()),
    )
    service = GroupService(
        repo=repo,
        permission_service=PermissionServiceStub(),
        add_group_member_topic=topic,
    )
    service._test_group_id = group_id
    return service, members_repo, invitation_repo, topic


async def _async_value(value):
    return value


def _user(email: str, status: UserStatus = UserStatus.ACTIVE) -> User:
    return User(id=uuid4(), name="Alex", email=email, status=status)


def test_member_schema_only_accepts_email_and_role() -> None:
    member = GroupMemberCreate(email="alex@example.com", role=GroupRole.MEMBER)
    assert str(member.email) == "alex@example.com"
    assert member.role == GroupRole.MEMBER


def test_validation_helpers_reject_invalid_roles_and_duplicate_emails() -> None:
    ctx = _ctx()
    with pytest.raises(BadRequestException, match="Only member"):
        GroupService._validate_member_roles(
            [GroupMemberCreate(email="alex@example.com", role=GroupRole.ADMIN)], ctx
        )
    with pytest.raises(BadRequestException, match="duplicated"):
        GroupService._validate_unique_request_emails(
            [
                GroupMemberCreate(email="Alex@example.com", role=GroupRole.MEMBER),
                GroupMemberCreate(email="alex@example.com", role=GroupRole.GUEST),
            ],
            ctx,
        )


@pytest.mark.asyncio
async def test_invite_creates_pending_memberships_and_stores_invitations() -> None:
    user = _user("alex@example.com")
    service, members_repo, invitation_repo, topic = _service([user])
    credential = Credential(
        id=uuid4(), email="admin@example.com", status=UserStatus.ACTIVE
    )

    invitations = await service.invite_group_members(
        group_id=service._test_group_id,
        members=[GroupMemberCreate(email=user.email, role=GroupRole.MODERATOR)],
        credential=credential,
        ctx=_ctx(),
    )

    assert len(members_repo.created) == 1
    assert members_repo.created[0].invitation_status == GroupMemberInvitationStatus.PENDING
    assert members_repo.created[0].invitation_expires_at == invitations[0].expires_at
    assert invitation_repo.saved == invitations
    assert topic.messages[0].invitation_id == invitations[0].invitation_id


@pytest.mark.asyncio
async def test_invite_rejects_missing_inactive_and_existing_members() -> None:
    service, _, _, _ = _service([])
    credential = Credential(id=uuid4(), email="admin@example.com", status=UserStatus.ACTIVE)
    with pytest.raises(BadRequestException, match="do not exist"):
        await service.invite_group_members(
            group_id=service._test_group_id,
            members=[GroupMemberCreate(email="missing@example.com", role=GroupRole.MEMBER)],
            credential=credential,
            ctx=_ctx(),
        )

    inactive_user = _user("inactive@example.com", UserStatus.INACTIVE)
    service, _, _, _ = _service([inactive_user])
    with pytest.raises(BadRequestException, match="ACTIVE or PENDING"):
        await service.invite_group_members(
            group_id=service._test_group_id,
            members=[GroupMemberCreate(email=inactive_user.email, role=GroupRole.MEMBER)],
            credential=credential,
            ctx=_ctx(),
        )

    existing_user = _user("member@example.com")
    service, _, _, _ = _service([existing_user], {existing_user.id})
    with pytest.raises(BadRequestException, match="already group members"):
        await service.invite_group_members(
            group_id=service._test_group_id,
            members=[GroupMemberCreate(email=existing_user.email, role=GroupRole.MEMBER)],
            credential=credential,
            ctx=_ctx(),
        )


@pytest.mark.asyncio
async def test_group_existence_validation_rejects_a_missing_group() -> None:
    service, _, _, _ = _service([])
    service.repo.group_repo = lambda: SimpleNamespace(
        get_group=lambda **kwargs: _async_value(None)
    )
    with pytest.raises(NotFoundException):
        await service._validate_group_exists(uuid4(), SimpleNamespace(), _ctx())


@pytest.mark.asyncio
async def test_delivery_failures_do_not_undo_created_invitations() -> None:
    user = _user("alex@example.com")
    service, members_repo, invitation_repo, _ = _service([user])
    credential = Credential(id=uuid4(), email="admin@example.com", status=UserStatus.ACTIVE)

    async def fail_publish(message):
        raise RuntimeError("RabbitMQ unavailable")

    service.add_group_member_topic.publish_invitation = fail_publish
    await service.invite_group_members(
        group_id=service._test_group_id,
        members=[GroupMemberCreate(email=user.email, role=GroupRole.MEMBER)],
        credential=credential,
        ctx=_ctx(),
    )
    assert len(members_repo.created) == 1
    assert len(invitation_repo.saved) == 1

    user = _user("redis@example.com")
    service, members_repo, invitation_repo, topic = _service([user])

    async def fail_save(invitation, ctx):
        raise RuntimeError("Redis unavailable")

    invitation_repo.replace_pending_invitation = fail_save
    await service.invite_group_members(
        group_id=service._test_group_id,
        members=[GroupMemberCreate(email=user.email, role=GroupRole.MEMBER)],
        credential=credential,
        ctx=_ctx(),
    )
    assert len(members_repo.created) == 1
    assert topic.messages == []


@pytest.mark.asyncio
async def test_invitation_acceptance_activates_and_consumes_membership() -> None:
    user = _user("alex@example.com")
    service, _, invitation_repo, _ = _service([user])
    invitation = (
        await service.invite_group_members(
            group_id=service._test_group_id,
            members=[GroupMemberCreate(email=user.email, role=GroupRole.MEMBER)],
            credential=Credential(
                id=uuid4(), email="admin@example.com", status=UserStatus.ACTIVE
            ),
            ctx=_ctx(),
        )
    )[0]
    result = await service.validate_group_invitation(
        invitation.invitation_id,
        Credential(id=user.id, email=user.email, status=UserStatus.ACTIVE),
        _ctx(),
    )
    assert result == "Invitation accepted"
    member = service.repo.group_members_repo().created[0]
    assert member.invitation_status == GroupMemberInvitationStatus.ACCEPTED
    assert member.invitation_id is None
    assert await invitation_repo.get(invitation.invitation_id, _ctx()) is None

    await invitation_repo.replace_pending_invitation(invitation, _ctx())
    with pytest.raises(ForbiddenException):
        await service.validate_group_invitation(
            invitation.invitation_id,
            Credential(id=uuid4(), email="other@example.com", status=UserStatus.ACTIVE),
            _ctx(),
        )
    await invitation_repo.consume(invitation, _ctx())
    with pytest.raises(NotFoundException):
        await service.validate_group_invitation(
            invitation.invitation_id,
            Credential(id=user.id, email=user.email, status=UserStatus.ACTIVE),
            _ctx(),
        )


@pytest.mark.asyncio
async def test_invitation_acceptance_is_successful_for_an_existing_member() -> None:
    user = _user("alex@example.com")
    service, members_repo, invitation_repo, _ = _service([user], {user.id})
    invitation = GroupInvitationInfo(
        invitation_id=uuid4(),
        group_id=service._test_group_id,
        member_id=user.id,
        email=user.email,
        role=GroupRole.MEMBER,
        group_name="Weekend plans",
        invited_by=uuid4(),
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc),
    )
    await invitation_repo.replace_pending_invitation(invitation, _ctx())

    result = await service.validate_group_invitation(
        invitation.invitation_id,
        Credential(id=user.id, email=user.email, status=UserStatus.ACTIVE),
        _ctx(),
    )

    assert result == "Invitation accepted"
    assert members_repo.created == []
    assert await invitation_repo.get(invitation.invitation_id, _ctx()) is None


@pytest.mark.asyncio
async def test_new_invitation_replaces_the_previous_pending_invitation() -> None:
    user = _user("alex@example.com")
    service, _, invitation_repo, _ = _service([user])
    credential = Credential(id=uuid4(), email="admin@example.com", status=UserStatus.ACTIVE)

    first = await service.invite_group_members(
        group_id=service._test_group_id,
        members=[GroupMemberCreate(email=user.email, role=GroupRole.MEMBER)],
        credential=credential,
        ctx=_ctx(),
    )
    second = await service.invite_group_members(
        group_id=service._test_group_id,
        members=[GroupMemberCreate(email=user.email, role=GroupRole.GUEST)],
        credential=credential,
        ctx=_ctx(),
    )

    assert await invitation_repo.get(first[0].invitation_id, _ctx()) is None
    assert await invitation_repo.get(second[0].invitation_id, _ctx()) == second[0]
    member = service.repo.group_members_repo().created[0]
    assert member.role == GroupRole.GUEST
    assert member.invitation_status == GroupMemberInvitationStatus.PENDING


@pytest.mark.asyncio
async def test_expired_pending_memberships_are_rejected_and_can_be_reinvited() -> None:
    user = _user("alex@example.com")
    service, members_repo, _, _ = _service([user])
    credential = Credential(
        id=uuid4(), email="admin@example.com", status=UserStatus.ACTIVE
    )
    await service.invite_group_members(
        group_id=service._test_group_id,
        members=[GroupMemberCreate(email=user.email, role=GroupRole.MEMBER)],
        credential=credential,
        ctx=_ctx(),
    )
    member = members_repo.created[0]
    member.invitation_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    assert await service.reject_expired_group_invitations(_ctx()) == 1
    assert member.invitation_status == GroupMemberInvitationStatus.REJECTED

    reinvitation = await service.invite_group_members(
        group_id=service._test_group_id,
        members=[GroupMemberCreate(email=user.email, role=GroupRole.GUEST)],
        credential=credential,
        ctx=_ctx(),
    )
    assert len(members_repo.created) == 1
    assert member.invitation_status == GroupMemberInvitationStatus.PENDING
    assert member.role == GroupRole.GUEST
    assert member.invitation_id == reinvitation[0].invitation_id


@pytest.mark.asyncio
async def test_group_invitation_topic_renders_acceptance_url() -> None:
    class MailServiceStub:
        request = None

        async def send(self, request, ctx):
            self.request = request
            return SendMailResponse(success=True, message="sent")

    mail_service = MailServiceStub()
    topic = AddGroupMemberTopic(SimpleNamespace(), mail_service)  # type: ignore[arg-type]
    invitation_id = uuid4()
    await topic._handle_message(
        TopicMessage(
            payload={
                "invitation_id": str(invitation_id),
                "email": "alex@example.com",
                "group_name": "Weekend plans",
                "role": "member",
            },
            routing_key="test",
        )
    )
    assert "alex@example.com" in mail_service.request.html
    assert str(invitation_id) in mail_service.request.html
    assert f"/invitation/{invitation_id}" in mail_service.request.html
