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
    GroupMemberListInfo,
    GroupMemberListQuery,
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
from sqlalchemy.exc import IntegrityError
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
        """Initialize group workflows and their infrastructure dependencies.

        Args:
            repo: Registry providing group, user, membership, and invitation storage.
            permission_service: Resolves group-scoped roles for protected actions.
            add_group_member_topic: Publishes invitation-email delivery requests.
        """
        self.repo = repo
        self.permission_service = permission_service
        self.add_group_member_topic = add_group_member_topic

    async def _validate_group_exists(
        self, group_id: UUID, session: AsyncSession, ctx: AppContext
    ) -> Group:
        """Load the target group or reject an invitation for an unknown group.

        Args:
            group_id: Identifier of the group receiving new members.
            session: Active transaction-scoped database session.
            ctx: Request trace context for repository logging.

        Returns:
            The persisted group.

        Raises:
            NotFoundException: If no group matches ``group_id``.
        """
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
        """Ensure every requested invitation has an assignable role.

        Owners and administrators cannot be assigned through invitations. Only
        member, moderator, and guest roles are accepted.

        Args:
            members: Requested recipients and their intended group roles.
            ctx: Request trace context for error logging.

        Raises:
            BadRequestException: If any requested role is not assignable.
        """
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
        """Normalize and ensure emails occur once within an invitation batch.

        Args:
            members: Requested recipients.
            ctx: Request trace context for error logging.

        Returns:
            Lowercase recipient email addresses in request order.

        Raises:
            BadRequestException: If the request contains the same email twice.
        """
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
        """Resolve invitation recipients by normalized email in one repository call.

        Args:
            members: Requested recipients.
            session: Active transaction-scoped database session.
            ctx: Request trace context for repository logging.

        Returns:
            Users keyed by their lowercase email address.
        """
        emails = [str(member.email).lower() for member in members]
        users = await self.repo.user_repo().get_by_emails(
            session=session, emails=emails, ctx=ctx
        )
        return {user.email.lower(): user for user in users}

    @staticmethod
    def _validate_users_found(
        users_by_email: Dict[str, User], requested_emails: List[str], ctx: AppContext
    ) -> None:
        """Ensure every requested email belongs to an existing account.

        Args:
            users_by_email: Resolved users keyed by normalized email.
            requested_emails: Normalized emails submitted by the caller.
            ctx: Request trace context reserved for validation logging.

        Raises:
            BadRequestException: If one or more recipients do not exist.
        """
        missing_emails = set(requested_emails) - set(users_by_email)
        if missing_emails:
            raise BadRequestException(message="One or more invited users do not exist")

    @staticmethod
    def _validate_invitable_user_statuses(
        users_by_email: Dict[str, User], ctx: AppContext
    ) -> None:
        """Restrict invitations to ACTIVE and PENDING accounts.

        Args:
            users_by_email: Resolved invitation recipients.
            ctx: Request trace context reserved for validation logging.

        Raises:
            BadRequestException: If any account is inactive, deleted, or otherwise
                not eligible for a group invitation.
        """
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
        """Reject a batch containing users already in the target group.

        Args:
            group_id: Target group identifier.
            users: Resolved users being invited.
            session: Active transaction-scoped database session.
            ctx: Request trace context for repository logging.

        Raises:
            BadRequestException: If any requested user already has membership.
        """
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
        """Create eligible users' invitations and request email delivery.

        Authorization is enforced by ``require_permission`` against ``group_id``.
        All request validation occurs before invitations are persisted. Membership
        creation is deliberately deferred until the invited user validates their
        invitation.

        Args:
            group_id: Group receiving the new memberships.
            members: Existing-user email addresses and allowed group roles.
            credential: Authenticated caller sending the invitations.
            ctx: Request trace and authorization context.

        Returns:
            Invitation metadata generated for every validated recipient.

        Raises:
            BadRequestException: If the batch is empty, invalid, duplicated, has
                unknown/ineligible users, or includes existing members.
            ForbiddenException: If the caller lacks Owner or Admin permission for
                the target group.
            NotFoundException: If the target group does not exist.
        """
        if not members or len(members) == 0:
            raise BadRequestException(message="At least one member is required")

        async def validate_invitation_request(
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
            return invitations

        invitations = await self.repo.transaction_wrapper(validate_invitation_request)
        for invitation in invitations:
            try:
                await self.repo.group_invitation_repo().replace_pending_invitation(
                    invitation, ctx
                )
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

    async def validate_group_invitation(
        self, invitation_id: UUID, credential: Credential, ctx: AppContext
    ) -> str:
        """Accept an invitation and create its membership from the Redis payload.

        The stored invitation is trusted after recipient-email validation. Existing
        memberships are treated as a successful idempotent acceptance.

        Args:
            invitation_id: Invitation identifier from the frontend route.
            credential: Authenticated user attempting to view the invitation.
            ctx: Request trace context for logging and Redis access.

        Returns:
            A confirmation message.

        Raises:
            NotFoundException: If the invitation is missing or expired.
            ForbiddenException: If the invitation belongs to another email address.
        """
        invitation = await self.repo.group_invitation_repo().get(invitation_id, ctx)
        if invitation is None:
            logger.error(
                msg=f"Invitation with id {invitation_id} not found", context=ctx
            )
            raise NotFoundException(message="Invitation not found or expired")
        if not secrets.compare_digest(
            str(invitation.email).lower(), credential.email.lower()
        ):
            logger.error(
                msg=f"User {credential.email} is not authorized to view invitation {invitation_id}",
                context=ctx,
            )
            raise ForbiddenException(message="You cannot view this invitation")

        async def create_membership(session: AsyncSession) -> GroupMembers | None:
            existing_member = (
                await self.repo.group_members_repo().get_group_member_by_id(
                    session=session,
                    member_id=invitation.member_id,
                    group_id=invitation.group_id,
                )
            )
            if existing_member is not None:
                return existing_member
            member = GroupMembers(
                member_id=invitation.member_id,
                group_id=invitation.group_id,
                role=invitation.role,
            )
            await self.repo.group_members_repo().create(
                session=session, group_members=[member], ctx=ctx
            )
            return member

        try:
            group_member = await self.repo.transaction_wrapper(create_membership)
        except IntegrityError:

            async def get_existing_membership(
                session: AsyncSession,
            ) -> GroupMembers | None:
                return await self.repo.group_members_repo().get_group_member_by_id(
                    session=session,
                    member_id=invitation.member_id,
                    group_id=invitation.group_id,
                )

            group_member = await self.repo.transaction_wrapper(get_existing_membership)
            if group_member is None:
                raise

        await self.repo.group_members_repo().set_group_member_redis(
            member=group_member, ctx=ctx
        )
        await self.repo.group_invitation_repo().consume(invitation, ctx)
        return "Invitation accepted"

    @staticmethod
    def _to_group_info(group: Group, members: List[User]) -> GroupInfo:
        """Map a group and its member ORM entities to the public group schema.

        Args:
            group: Persisted group entity.
            members: Users currently associated with the group.

        Returns:
            API-safe group information with member profile fields.
        """
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
        """Create initial group membership rows and warm their Redis cache entries.

        The creator is always included. During group creation, all supplied users
        receive the owner role because that is the existing group-creation policy.

        Args:
            member_ids: User IDs supplied during group creation.
            group_id: Newly created group identifier.
            ctx: Request trace context for persistence and cache logging.
            session: Active transaction-scoped database session.
            credential: Authenticated creator, who is always included.
            is_owner_create: Whether this creation flow assigns the owner role.
        """
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
        """Create a group, assign its initial members, and select it for its owner.

        Args:
            group_create: Requested group name, description, and initial members.
            credential: Authenticated user who becomes the group owner.
            ctx: Request trace context for persistence and cache operations.

        Returns:
            The created group with its member profiles.

        Raises:
            BadRequestException: If the group name is missing.
        """

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
        """List the current user's groups as id/label pairs.

        Args:
            ctx: Request trace context for repository logging.
            credential: Authenticated user whose memberships are queried.

        Returns:
            Group identifiers and names suitable for a selection control.
        """

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

    @require_permission(GroupRole.ADMIN)
    async def list_group_members(
        self,
        query: GroupMemberListQuery,
        credential: Credential,
        ctx: AppContext,
        group_id: UUID | None = None,
    ) -> tuple[List[GroupMemberListInfo], int]:
        """Return an authorized, filtered page of group memberships."""

        async def _list_group_members(
            session: AsyncSession,
        ) -> tuple[List[GroupMemberListInfo], int]:
            await self._validate_group_exists(query.group_id, session, ctx)
            rows, total = await self.repo.group_members_repo().list_group_members(
                session=session, query=query, ctx=ctx
            )
            return [
                GroupMemberListInfo(
                    member_id=member.member_id,
                    image_url=user.image_url,
                    email=user.email,
                    name=user.name,
                    joined_at=member.created_at,
                    role=member.role,
                    status=user.status,
                )
                for member, user in rows
            ], total

        return await self.repo.transaction_wrapper(_list_group_members)

    async def get_group(
        self, group_id: UUID, ctx: AppContext, credential: Credential
    ) -> GroupInfo:
        """Return group details when the caller is a member of that group.

        Args:
            group_id: Requested group identifier.
            ctx: Request trace context for repository logging.
            credential: Authenticated user requesting group details.

        Returns:
            The group and its members.

        Raises:
            BadRequestException: If the group is absent or the caller is not a member.
        """

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
        """Set the caller's active group after confirming their membership.

        Args:
            input: Requested active group identifier.
            ctx: Request trace context for repository logging.
            credential: Authenticated user switching groups.

        Raises:
            BadRequestException: If the group is absent or the caller is not a member.
        """

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
