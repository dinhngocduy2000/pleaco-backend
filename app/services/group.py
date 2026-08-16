import asyncio
import secrets
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from uuid import UUID, uuid4
from app.common.context import AppContext
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
)
from app.common.middleware.logger import Logger
from app.common.schemas.common import HashMapResponse
from app.common.schemas.group import (
    GroupCreateDTO,
    GroupCreateDomain,
    GroupInvitationInfo,
    GroupInfo,
    GroupMemberCreate,
    GroupQuery,
)
from app.common.schemas.user import Credential, SwitchGroupRequest, UserInfo, UserUpdate
from app.core.rbac.permissions import PermissionService
from app.core.rbac.role_validation import require_permission
from app.core.config import settings
from app.external.queues.topics.add_group_member import (
    AddGroupMemberMessage,
    AddGroupMemberTopic,
)
from app.models.group import Group
from app.models.group_members import GroupMembers
from app.models.user import User
from app.repository.registry import Registry
from sqlalchemy.ext.asyncio import AsyncSession

logger = Logger()


class GroupService:
    repo: Registry
    permission_service: PermissionService
    add_group_member_topic: AddGroupMemberTopic

    def __init__(
        self,
        repo: Registry,
        permission_service: PermissionService,
        add_group_member_topic: AddGroupMemberTopic,
    ) -> None:
        self.repo = repo
        self.permission_service = permission_service
        self.add_group_member_topic = add_group_member_topic

    async def _validate_group_exists(
        self, group_id: UUID, session: AsyncSession, ctx: AppContext
    ) -> Group:
        group = await self.repo.group_repo().get_group(
            session=session, query=GroupQuery(id=group_id), ctx=ctx
        )
        if group is None:
            raise NotFoundException(message="Group not found")
        return group

    @staticmethod
    def _validate_member_roles(
        members: List[GroupMemberCreate], ctx: AppContext
    ) -> None:
        allowed_roles = {GroupRole.MEMBER, GroupRole.MODERATOR, GroupRole.GUEST}
        invalid_roles = {member.role for member in members} - allowed_roles
        if invalid_roles:
            logger.error(msg=f"Invalid member roles: {invalid_roles}", context=ctx)
            raise BadRequestException(
                message="Only member, moderator, and guest roles can be invited"
            )

    @staticmethod
    def _validate_unique_request_emails(
        members: List[GroupMemberCreate], ctx: AppContext
    ) -> List[str]:
        emails = [str(member.email).lower() for member in members]
        if len(emails) != len(set(emails)):
            logger.error(msg="Duplicate member emails are not allowed", context=ctx)
            raise BadRequestException(
                message="Some of the invited emails are duplicated"
            )
        return emails

    async def _get_invited_users_by_email(
        self,
        members: List[GroupMemberCreate],
        session: AsyncSession,
        ctx: AppContext,
    ) -> Dict[str, User]:
        emails = [str(member.email).lower() for member in members]
        users = await self.repo.user_repo().get_by_emails(
            session=session, emails=emails, ctx=ctx
        )
        return {user.email.lower(): user for user in users}

    @staticmethod
    def _validate_users_found(
        users_by_email: Dict[str, User], requested_emails: List[str], ctx: AppContext
    ) -> None:
        missing_emails = set(requested_emails) - set(users_by_email)
        if missing_emails:
            raise BadRequestException(message="One or more invited users do not exist")

    @staticmethod
    def _validate_invitable_user_statuses(
        users_by_email: Dict[str, User], ctx: AppContext
    ) -> None:
        allowed_statuses = {UserStatus.ACTIVE, UserStatus.PENDING}
        if any(user.status not in allowed_statuses for user in users_by_email.values()):
            raise BadRequestException(
                message="Invited users must have ACTIVE or PENDING status"
            )

    async def _validate_users_not_already_group_members(
        self,
        group_id: UUID,
        users: List[User],
        session: AsyncSession,
        ctx: AppContext,
    ) -> None:
        existing_member_ids = (
            await self.repo.group_members_repo().list_existing_member_ids(
                session=session,
                group_id=group_id,
                member_ids=[user.id for user in users],
                ctx=ctx,
            )
        )
        if existing_member_ids:
            raise BadRequestException(
                message="One or more invited users are already group members"
            )

    @require_permission(GroupRole.ADMIN)
    async def invite_group_members(
        self,
        group_id: UUID,
        members: List[GroupMemberCreate],
        credential: Credential,
        ctx: AppContext,
    ) -> List[GroupInvitationInfo]:
        """Add valid users to a group and enqueue their invitation emails."""
        if not members or len(members) == 0:
            raise BadRequestException(message="At least one member is required")

        async def create_memberships(
            session: AsyncSession,
        ) -> List[GroupInvitationInfo]:
            group = await self._validate_group_exists(group_id, session, ctx)
            self._validate_member_roles(members, ctx)
            requested_emails = self._validate_unique_request_emails(members, ctx)
            users_by_email = await self._get_invited_users_by_email(
                members, session, ctx
            )
            self._validate_users_found(users_by_email, requested_emails, ctx)
            self._validate_invitable_user_statuses(users_by_email, ctx)
            users = [users_by_email[email] for email in requested_emails]
            await self._validate_users_not_already_group_members(
                group_id, users, session, ctx
            )

            now = datetime.now(timezone.utc)
            expires_at = now + timedelta(seconds=settings.INVITATION_EXPIRE_SECONDS)
            invitations = [
                GroupInvitationInfo(
                    invitation_id=uuid4(),
                    group_id=group_id,
                    member_id=users_by_email[str(member.email).lower()].id,
                    email=str(member.email).lower(),
                    role=member.role,
                    group_name=group.name,
                    invited_by=credential.id,
                    created_at=now,
                    expires_at=expires_at,
                )
                for member in members
            ]
            group_members = [
                GroupMembers(
                    member_id=invitation.member_id,
                    group_id=group_id,
                    role=invitation.role,
                )
                for invitation in invitations
            ]
            await self.repo.group_members_repo().create(
                session=session, group_members=group_members, ctx=ctx
            )
            await asyncio.gather(
                *(
                    self.repo.group_members_repo().set_group_member_redis(
                        member=member, ctx=ctx
                    )
                    for member in group_members
                )
            )
            return invitations

        invitations = await self.repo.transaction_wrapper(create_memberships)
        for invitation in invitations:
            try:
                await self.repo.group_invitation_repo().save(invitation, ctx)
            except Exception:
                logger.exception(
                    msg="Unable to store group invitation; email will not be queued",
                    context=ctx,
                )
                continue
            try:
                await self.add_group_member_topic.publish_invitation(
                    AddGroupMemberMessage(
                        invitation_id=invitation.invitation_id,
                        email=invitation.email,
                        group_name=invitation.group_name,
                        role=invitation.role,
                    )
                )
            except Exception:
                logger.exception(
                    msg="Unable to queue group invitation email", context=ctx
                )
        return invitations

    async def get_group_invitation(
        self, invitation_id: UUID, credential: Credential, ctx: AppContext
    ) -> GroupInvitationInfo:
        invitation = await self.repo.group_invitation_repo().get(invitation_id, ctx)
        if invitation is None:
            raise NotFoundException(message="Invitation not found or expired")
        if not secrets.compare_digest(
            str(invitation.email).lower(), credential.email.lower()
        ):
            raise ForbiddenException(message="You cannot view this invitation")
        return invitation

    @staticmethod
    def _to_group_info(group: Group, members: List[User]) -> GroupInfo:
        return GroupInfo(
            id=group.id,
            name=group.name,
            created_at=group.created_at,
            updated_at=group.updated_at,
            members=[
                UserInfo(
                    id=member.id,
                    name=member.name,
                    email=member.email,
                    status=member.status.value,
                    created_at=member.created_at,
                    updated_at=member.updated_at,
                    image_url=member.image_url,
                    group_id=member.active_group_id,
                )
                for member in members
            ],
        )

    async def _create_group_members(
        self,
        member_ids: List[UUID],
        group_id: UUID,
        ctx: AppContext,
        session: AsyncSession,
        credential: Credential,
        is_owner_create: Optional[bool] = None,
    ) -> None:
        member_ids = dict.fromkeys([*(member_ids or []), credential.id])
        new_group_members = [
            GroupMembers(
                member_id=member_id,
                group_id=group_id,
                role=GroupRole.OWNER if is_owner_create else GroupRole.MEMBER,
            )
            for member_id in member_ids
        ]

        await self.repo.group_members_repo().create(
            group_members=new_group_members,
            ctx=ctx,
            session=session,
        )
        await asyncio.gather(
            *(
                self.repo.group_members_repo().set_group_member_redis(
                    member=group_member,
                    ctx=ctx,
                )
                for group_member in new_group_members
            )
        )

    async def create_group(
        self, group_create: GroupCreateDTO, credential: Credential, ctx: AppContext
    ) -> GroupInfo:
        async def _create_group(session: AsyncSession) -> GroupInfo:
            try:
                if group_create.name is None:
                    logger.error(msg=f"Group's name is required", context=ctx)
                    raise BadRequestException(message="Group's name is required")

                group_create_domain = GroupCreateDomain(
                    name=group_create.name,
                    description=group_create.description,
                    owner_id=credential.id,
                )
                new_group = await self.repo.group_repo().create_group(
                    session=session, group_create=group_create_domain, ctx=ctx
                )

                await self._create_group_members(
                    member_ids=group_create.members,
                    group_id=new_group.id,
                    ctx=ctx,
                    session=session,
                    credential=credential,
                    is_owner_create=True,
                ),

                logger.info(msg=f"Group created successfully", context=ctx)
                await self.repo.group_repo().set_group_owner(
                    group_id=new_group.id, group_owner=credential.id, ctx=ctx
                )
                await self.repo.user_repo().update_user(
                    session=session,
                    user_id=credential.id,
                    user_update=UserUpdate(active_group_id=new_group.id),
                    ctx=ctx,
                )
                # current_user = await self.repo.user_repo().get_user_profile

                group_with_members = await self.repo.group_repo().get_group(
                    session=session,
                    query=GroupQuery(id=new_group.id),
                    ctx=ctx,
                )
                members = await self.repo.group_repo().list_member_users(
                    session=session, group_id=new_group.id, ctx=ctx
                )
                return self._to_group_info(group_with_members, members)
            except Exception as e:
                logger.error(msg=f"Create group: Exception: {e}", context=ctx)
                raise e

        return await self.repo.transaction_wrapper(_create_group)

    async def list_group_key_value(
        self, ctx: AppContext, credential: Credential
    ) -> List[HashMapResponse]:
        async def _list_group_key_value(session: AsyncSession) -> List[Dict[UUID, str]]:
            try:
                query = GroupQuery(members=[credential.id])
                groups = await self.repo.group_repo().list_group_map(
                    session=session, query=query, ctx=ctx
                )
                logger.info(msg=f"Groups: {groups}", context=ctx)
                return [
                    HashMapResponse(value=group["id"], label=group["name"])
                    for group in groups
                ]
            except Exception as e:
                logger.error(
                    msg=f"List group key value service: Exception: {e}", context=ctx
                )
                raise e

        return await self.repo.transaction_wrapper(_list_group_key_value)

    async def get_group(
        self, group_id: UUID, ctx: AppContext, credential: Credential
    ) -> GroupInfo:
        async def _get_group(session: AsyncSession) -> GroupInfo:
            try:
                group = await self.repo.group_repo().get_group(
                    session=session,
                    query=GroupQuery(id=group_id),
                    ctx=ctx,
                )
                if group is None:
                    logger.error(msg=f"Group with id {group_id} not found", context=ctx)
                    raise BadRequestException(message="Group not found")
                members = await self.repo.group_repo().list_member_users(
                    session=session, group_id=group.id, ctx=ctx
                )
                if credential.id not in {member.id for member in members}:
                    logger.error(msg=f"User is not a member of the group", context=ctx)
                    raise BadRequestException(
                        message="User is not a member of the group"
                    )
                return self._to_group_info(group, members)
            except Exception as e:
                logger.error(msg=f"Get group service: Exception: {e}", context=ctx)
                raise e

        return await self.repo.transaction_wrapper(_get_group)

    async def switch_current_user_active_group(
        self, input: SwitchGroupRequest, ctx: AppContext, credential: Credential
    ) -> None:
        async def _switch_current_user_active_group(session: AsyncSession) -> None:
            try:

                group = await self.repo.group_repo().get_group(
                    session=session,
                    query=GroupQuery(id=input.group_id),
                    ctx=ctx,
                )

                if group is None:
                    logger.error(
                        msg=f"Group with id {input.group_id} not found", context=ctx
                    )
                    raise BadRequestException(message="Group not found")

                members = await self.repo.group_repo().list_member_users(
                    session=session, group_id=group.id, ctx=ctx
                )
                if credential.id not in {member.id for member in members}:
                    logger.error(msg=f"User is not a member of the group", context=ctx)
                    raise BadRequestException(
                        message="User is not a member of the group"
                    )

                await self.repo.user_repo().update_user(
                    session=session,
                    user_id=ctx.actor,
                    user_update=UserUpdate(active_group_id=input.group_id),
                    ctx=ctx,
                )
                return
            except Exception as e:
                logger.error(msg=f"Switch group service: Exception: {e}", context=ctx)
                raise e

        return await self.repo.transaction_wrapper(_switch_current_user_active_group)
