from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.common.context import AppContext
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
from app.models.user import User
from app.services.group import GroupService


def _ctx() -> AppContext:
    return AppContext(trace_id=uuid4(), action=INVITE_MEMBER, actor=uuid4())


class GroupMembersRepositoryStub:
    def __init__(self, existing_member_ids: set | None = None) -> None:
        self.existing_member_ids = existing_member_ids or set()
        self.created = []

    async def list_existing_member_ids(self, **kwargs):
        return self.existing_member_ids

    async def create(self, *, group_members, **kwargs):
        self.created.extend(group_members)
        return group_members

    async def set_group_member_redis(self, **kwargs):
        return None


class GroupInvitationRepositoryStub:
    def __init__(self) -> None:
        self.saved = []
        self.invitation = None

    async def save(self, invitation, ctx):
        self.saved.append(invitation)

    async def get(self, invitation_id, ctx):
        return self.invitation


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
    with pytest.raises(BadRequestException, match="Duplicate"):
        GroupService._validate_unique_request_emails(
            [
                GroupMemberCreate(email="Alex@example.com", role=GroupRole.MEMBER),
                GroupMemberCreate(email="alex@example.com", role=GroupRole.GUEST),
            ],
            ctx,
        )


@pytest.mark.asyncio
async def test_invite_creates_memberships_stores_invitations_and_publishes() -> None:
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
    assert members_repo.created[0].role == GroupRole.MODERATOR
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
async def test_delivery_failures_do_not_undo_created_memberships() -> None:
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

    invitation_repo.save = fail_save
    await service.invite_group_members(
        group_id=service._test_group_id,
        members=[GroupMemberCreate(email=user.email, role=GroupRole.MEMBER)],
        credential=credential,
        ctx=_ctx(),
    )
    assert len(members_repo.created) == 1
    assert topic.messages == []


@pytest.mark.asyncio
async def test_invitation_lookup_is_limited_to_the_invited_user() -> None:
    user = _user("alex@example.com")
    service, _, invitation_repo, _ = _service([user])
    invitation_repo.invitation = GroupInvitationInfo(
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
    invitation = await service.get_group_invitation(
        invitation_repo.invitation.invitation_id,
        Credential(id=user.id, email=user.email, status=UserStatus.ACTIVE),
        _ctx(),
    )
    assert invitation == invitation_repo.invitation
    with pytest.raises(ForbiddenException):
        await service.get_group_invitation(
            invitation.invitation_id,
            Credential(id=uuid4(), email="other@example.com", status=UserStatus.ACTIVE),
            _ctx(),
        )
    invitation_repo.invitation = None
    with pytest.raises(NotFoundException):
        await service.get_group_invitation(invitation.invitation_id, Credential(id=user.id, email=user.email, status=UserStatus.ACTIVE), _ctx())


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
