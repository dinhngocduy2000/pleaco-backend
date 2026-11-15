from typing import Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from app.common.context import AppContext
from app.common.enum.context_actions import (
    CREATE_BOT,
    SEND_ROBOT_COMMAND,
    PREVIEW_CLEANING_PATH,
    SAVE_DOCKING_STATIONS,
    ASSIGN_DOCKING_STATION_ROBOT,
    CREATE_ENVIRONMENT_ZONES,
    CREATE_MAP,
    UPDATE_MAP,
    SAVE_MAP_BOUNDARY,
    SAVE_MAP_LAYOUT,
    CREATE_TAG,
    DELETE_BOT,
    DELETE_GROUP,
    EDIT_MEMBER,
    EDIT_GROUP_SETTINGS,
    INVITE_MEMBER,
    LIST_BOTS,
    LIST_MAPS,
    LIST_TAGS,
    LIST_GROUP_MEMBERS,
    PROMOTE_TO_ADMIN,
    TRANSFER_OWNERSHIP,
    REMOVE_MEMBER,
)
from app.common.enum.user_roles import GroupRole
from app.common.exceptions import ForbiddenException
from app.common.middleware.logger import Logger
from app.common.schemas.group import GroupMemberInfo
from app.common.schemas.user import Credential
from app.repository.registry import DbAccessContext, Registry

ROLE_HIERACHY = [
    GroupRole.GUEST,
    GroupRole.MEMBER,
    GroupRole.MODERATOR,
    GroupRole.ADMIN,
    GroupRole.OWNER,
]

ROLE_PERMISSIONS: dict[GroupRole, set[str]] = {
    GroupRole.OWNER: {
        SAVE_DOCKING_STATIONS,
        ASSIGN_DOCKING_STATION_ROBOT,
        CREATE_ENVIRONMENT_ZONES,
        SAVE_MAP_BOUNDARY,
        SAVE_MAP_LAYOUT,
        CREATE_BOT,
        CREATE_TAG,
        CREATE_MAP,
        UPDATE_MAP,
        DELETE_BOT,
        LIST_BOTS,
        LIST_MAPS,
        LIST_TAGS,
        DELETE_GROUP,
        TRANSFER_OWNERSHIP,
        EDIT_GROUP_SETTINGS,
        INVITE_MEMBER,
        EDIT_MEMBER,
        REMOVE_MEMBER,
        PROMOTE_TO_ADMIN,
        LIST_GROUP_MEMBERS,
    },
    GroupRole.ADMIN: {
        SAVE_DOCKING_STATIONS,
        ASSIGN_DOCKING_STATION_ROBOT,
        CREATE_ENVIRONMENT_ZONES,
        SAVE_MAP_BOUNDARY,
        SAVE_MAP_LAYOUT,
        CREATE_BOT,
        CREATE_TAG,
        CREATE_MAP,
        UPDATE_MAP,
        DELETE_BOT,
        LIST_BOTS,
        LIST_MAPS,
        LIST_TAGS,
        EDIT_GROUP_SETTINGS,
        INVITE_MEMBER,
        EDIT_MEMBER,
        REMOVE_MEMBER,
        LIST_GROUP_MEMBERS,
    },
    GroupRole.MODERATOR: {
        CREATE_BOT,
        DELETE_BOT,
        LIST_BOTS,
        LIST_MAPS,
        LIST_TAGS,
    },
    GroupRole.MEMBER: {
        LIST_BOTS,
        LIST_MAPS,
        LIST_TAGS,
    },
    GroupRole.GUEST: {
        LIST_BOTS,
        LIST_MAPS,
        LIST_TAGS,
    },
}


OWN_ONLY_ACTIONS: set[str] = {}
for _role in (GroupRole.MEMBER, GroupRole.MODERATOR, GroupRole.ADMIN, GroupRole.OWNER):
    ROLE_PERMISSIONS[_role].add(SEND_ROBOT_COMMAND)
    # A preview reads the map and robot position but does not dispatch commands.
    ROLE_PERMISSIONS[_role].add(PREVIEW_CLEANING_PATH)
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
                # Redis may outlive a revoked membership. Always establish tenant
                # authority from the accepted database membership first.
                member = await self.repo.group_members_repo().get_group_member_by_id(
                    session=session,
                    member_id=credential.id,
                    group_id=target_group_id,
                    accepted_only=True,
                )
                if member is None:
                    raise ForbiddenException(
                        message="This user is not found in the current group"
                    )
                return GroupMemberInfo(
                    created_at=member.created_at,
                    updated_at=member.updated_at,
                    member_id=member.member_id,
                    group_id=member.group_id,
                    role=member.role,
                )

            except Exception as e:
                logger.error(
                    msg=f"Error getting group member when validating role: {e}",
                    context=ctx,
                )
                raise

        return await self.repo.transaction_wrapper(
            _get_group_member, DbAccessContext(actor_id=credential.id, group_id=None)
        )

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
