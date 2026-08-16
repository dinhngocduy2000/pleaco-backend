import asyncio
from typing import Dict, List, Optional
from uuid import UUID
from app.common.context import AppContext
from app.common.enum.user_roles import GroupRole
from app.common.exceptions import BadRequestException
from app.common.middleware.logger import Logger
from app.common.schemas.common import HashMapResponse
from app.common.schemas.group import (
    GroupCreateDTO,
    GroupCreateDomain,
    GroupInfo,
    GroupQuery,
)
from app.common.schemas.user import Credential, SwitchGroupRequest, UserInfo, UserUpdate
from app.core.rbac.permissions import PermissionService
from app.models.group import Group
from app.models.group_members import GroupMembers
from app.models.user import User
from app.repository.registry import Registry
from sqlalchemy.ext.asyncio import AsyncSession

logger = Logger()


class GroupService:
    repo: Registry
    permission_service: PermissionService

    def __init__(self, repo: Registry, permission_service: PermissionService) -> None:
        self.repo = repo
        self.permission_service = permission_service

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
                    raise BadRequestException(
                        message="Group's name is required")

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
                    logger.error(
                        msg=f"Group with id {group_id} not found", context=ctx)
                    raise BadRequestException(message="Group not found")
                members = await self.repo.group_repo().list_member_users(
                    session=session, group_id=group.id, ctx=ctx
                )
                if credential.id not in {member.id for member in members}:
                    logger.error(
                        msg=f"User is not a member of the group", context=ctx)
                    raise BadRequestException(
                        message="User is not a member of the group"
                    )
                return self._to_group_info(group, members)
            except Exception as e:
                logger.error(
                    msg=f"Get group service: Exception: {e}", context=ctx)
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
                    logger.error(
                        msg=f"User is not a member of the group", context=ctx)
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
                logger.error(
                    msg=f"Switch group service: Exception: {e}", context=ctx)
                raise e

        return await self.repo.transaction_wrapper(_switch_current_user_active_group)
