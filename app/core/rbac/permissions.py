from typing import Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from app.common.context import AppContext
from app.common.enum.context_actions import (
    CREATE_EVENT,
    CREATE_BOT,
    DELETE_EVENT,
    DELETE_GROUP,
    EDIT_MEMBER,
    EDIT_GROUP_SETTINGS,
    INVITE_MEMBER,
    LIST_CALENDAR_EVENTS,
    LIST_GROUP_MEMBERS,
    LOCK_EVENT,
    PROMOTE_TO_ADMIN,
    UPDATE_EVENT,
    VOTE_EVENT,
    TRANSFER_OWNERSHIP,
    REMOVE_MEMBER,
)
from app.common.enum.user_roles import GroupRole
from app.common.exceptions import ForbiddenException
from app.common.middleware.logger import Logger
from app.common.schemas.group import GroupMemberInfo
from app.common.schemas.user import Credential
from app.repository.registry import Registry

ROLE_HIERACHY = [
    GroupRole.GUEST,
    GroupRole.MEMBER,
    GroupRole.MODERATOR,
    GroupRole.ADMIN,
    GroupRole.OWNER,
]

ROLE_PERMISSIONS: dict[GroupRole, set[str]] = {
    GroupRole.OWNER: {
        CREATE_BOT,
        CREATE_EVENT,
        DELETE_EVENT,
        UPDATE_EVENT,
        DELETE_GROUP,
        VOTE_EVENT,
        LOCK_EVENT,
        TRANSFER_OWNERSHIP,
        EDIT_GROUP_SETTINGS,
        INVITE_MEMBER,
        EDIT_MEMBER,
        REMOVE_MEMBER,
        PROMOTE_TO_ADMIN,
        LIST_CALENDAR_EVENTS,
        LIST_GROUP_MEMBERS,
        # Action.VIEW_COST_DASHBOARD,
        # Action.SET_REMINDER,
        # Action.UPLOAD_MEMORY,
        # Action.LIKE_COMMENT,
        # Action.SET_PRIVACY,
        # Action.DELETE_MEMORY,
        # Action.EXPORT_PDF,
        # Action.SEND_MESSAGE,
        # Action.DELETE_MESSAGE,
        # Action.CONFIG_NOTIFICATION,
    },
    GroupRole.ADMIN: {
        CREATE_BOT,
        EDIT_GROUP_SETTINGS,
        INVITE_MEMBER,
        EDIT_MEMBER,
        REMOVE_MEMBER,
        CREATE_EVENT,
        DELETE_EVENT,
        UPDATE_EVENT,
        VOTE_EVENT,
        LIST_CALENDAR_EVENTS,
        LIST_GROUP_MEMBERS,
        LOCK_EVENT,
        # Action.SET_REMINDER,
        # Action.UPLOAD_MEMORY,
        # Action.LIKE_COMMENT,
        # Action.SET_PRIVACY,
        # Action.DELETE_MEMORY,
        # Action.EXPORT_PDF,
        # Action.SEND_MESSAGE,
        # Action.DELETE_MESSAGE,
        # Action.CONFIG_NOTIFICATION,
    },
    GroupRole.MODERATOR: {
        CREATE_BOT,
        # MEMBER ROLES
        CREATE_EVENT,
        VOTE_EVENT,
        LIST_CALENDAR_EVENTS,
        # Action.UPLOAD_MEMORY,
        # Action.LIKE_COMMENT,
        # Action.EXPORT_PDF,
        # Action.SEND_MESSAGE,
        # ----------------- content moderation — this is what separates them from member
        LOCK_EVENT,
        UPDATE_EVENT,
        DELETE_EVENT,
        # Action.SET_REMINDER,
        # Action.SET_PRIVACY,
        # Action.DELETE_MEMORY,
        # Action.DELETE_MESSAGE,
    },
    GroupRole.MEMBER: {
        CREATE_EVENT,
        VOTE_EVENT,
        LIST_CALENDAR_EVENTS,
        # Action.LIKE_COMMENT,
        # Action.UPLOAD_MEMORY,
        # Action.LIKE_COMMENT,
        # Action.EXPORT_PDF,
        # Action.SEND_MESSAGE,
    },
    GroupRole.GUEST: {
        LIST_CALENDAR_EVENTS,
    },
}


OWN_ONLY_ACTIONS: set[str] = {
    LOCK_EVENT,
    UPDATE_EVENT,
    DELETE_EVENT,
    # Action.SET_REMINDER,
    # Action.SET_PRIVACY,
    # Action.DELETE_MEMORY,
    # Action.DELETE_MESSAGE,
    # Action.UPLOAD_MEMORY,
    # Action.LIKE_COMMENT,
    # Action.SET_PRIVACY,
    # Action.DELETE_MEMORY,
}
logger = Logger()


class PermissionService:
    repo: Registry

    def __init__(self, repo: Registry) -> None:
        self.repo = repo

    async def get_group_member(
        self,
        credential: Credential,
        ctx: AppContext,
        group_id: UUID | None = None,
    ) -> Optional[GroupMemberInfo]:
        async def _get_group_member(session: AsyncSession) -> GroupMemberInfo:
            try:
                target_group_id = group_id or credential.active_group_id
                if target_group_id is None:
                    raise ForbiddenException(message="A group must be selected")
                member_info: GroupMemberInfo | None = None
                cached_member = (
                    await self.repo.group_members_repo().get_group_member_redis(
                        group_id=target_group_id,
                        member_id=credential.id,
                        ctx=ctx,
                    )
                )

                if not cached_member:
                    member = (
                        await self.repo.group_members_repo().get_group_member_by_id(
                            session=session,
                            member_id=credential.id,
                            group_id=target_group_id,
                            accepted_only=True,
                        )
                    )
                    if member is None:
                        logger.error(
                            "User Not found in Permission service", context=ctx
                        )
                        raise ForbiddenException(
                            message="This user is not found in the current group"
                        )
                    member_info = GroupMemberInfo(
                        created_at=member.created_at,
                        updated_at=member.updated_at,
                        member_id=member.member_id,
                        group_id=member.group_id,
                        role=member.role,
                    )
                    await self.repo.group_members_repo().set_group_member_redis(
                        member=member, ctx=ctx
                    )
                else:
                    member_info = GroupMemberInfo(
                        created_at=cached_member["created_at"],
                        updated_at=cached_member["updated_at"],
                        member_id=cached_member["member_id"],
                        group_id=cached_member["group_id"],
                        role=GroupRole(cached_member["role"]),
                    )

                return member_info

            except Exception as e:
                logger.error(
                    msg=f"Error getting group member when validating role: {e}",
                    context=ctx,
                )
                raise

        return await self.repo.transaction_wrapper(_get_group_member)

    @staticmethod
    def is_action_executable(
        role: GroupRole, action: str, is_owner: bool = False
    ) -> bool:
        if action in ROLE_PERMISSIONS[role]:
            return True

        # own-only fallback — only for member, moderator has these outright
        if role == GroupRole.MEMBER and action in OWN_ONLY_ACTIONS and is_owner:
            return True

        return False
