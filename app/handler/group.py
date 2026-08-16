from typing import Dict, List
from uuid import UUID, uuid4
from fastapi import Body, Depends, Path, Request
from app.common.context import AppContext
from app.common.enum.context_actions import (
    CREATE_GROUP,
    GET_GROUP_BY_ID,
    INVITE_MEMBER,
    LIST_GROUP_MEMBERS,
    LIST_GROUP_KEY_VALUE,
    SWITCH_CURRENT_USER_GROUP,
    VALIDATE_GROUP_INVITATION,
)
from app.common.exceptions.decorator import exception_handler
from app.common.middleware.auth_middleware import AuthMiddleware
from app.common.middleware.logger import Logger
from app.common.schemas.common import (
    BaseResponse,
    HashMapResponse,
    PaginationBaseResponse,
)
from app.common.schemas.group import (
    GroupCreateDTO,
    GroupInvitationInfo,
    GroupInfo,
    GroupMemberCreate,
    GroupMemberListInfo,
    GroupMemberListQuery,
)
from app.common.schemas.user import Credential, SwitchGroupRequest
from app.services.group import GroupService

logger = Logger()


class GroupHandler:
    service: GroupService

    def __init__(self, service: GroupService) -> None:
        self.service = service

    @exception_handler
    async def create_group(
        self,
        group_create: GroupCreateDTO,
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> GroupInfo:
        """
        Create a new group

        Args:
            group_create: Group creation data including name, description, and members

        Returns:
            GroupInfo: Created group information
        """
        ctx = AppContext(trace_id=uuid4(), action=CREATE_GROUP)
        group = await self.service.create_group(
            group_create, credential=credential, ctx=ctx
        )
        return BaseResponse[GroupInfo](
            data=group,
            message="Success",
            statusCode=201,
        )

    @exception_handler
    async def list_group_key_value(
        self,
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> BaseResponse[List[HashMapResponse]]:
        """
        List all groups with their IDs and names

        Returns:
            List[Dict[UUID, str]]: List of groups with their IDs and names
        """
        ctx = AppContext(trace_id=uuid4(), action=LIST_GROUP_KEY_VALUE)
        groups = await self.service.list_group_key_value(ctx=ctx, credential=credential)
        return BaseResponse[List[HashMapResponse]](
            data=groups,
            message="Success",
            statusCode=200,
        )

    @exception_handler
    async def get_group(
        self,
        group_id: UUID = Path(..., description="Group id"),
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> BaseResponse[GroupInfo]:
        """
        Get a group by its ID
        """
        ctx = AppContext(trace_id=uuid4(), action=GET_GROUP_BY_ID, actor=credential.id)
        group = await self.service.get_group(
            group_id=group_id, ctx=ctx, credential=credential
        )
        return BaseResponse[GroupInfo](
            data=group,
            message="Success",
            statusCode=200,
        )

    @exception_handler
    async def list_group_members(
        self,
        query: GroupMemberListQuery = Depends(),
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> PaginationBaseResponse[GroupMemberListInfo]:
        ctx = AppContext(
            trace_id=uuid4(), action=LIST_GROUP_MEMBERS, actor=credential.id
        )
        members, total = await self.service.list_group_members(
            query=query,
            group_id=query.group_id,
            credential=credential,
            ctx=ctx,
        )
        return PaginationBaseResponse[GroupMemberListInfo](
            total=total,
            page=query.page,
            page_size=query.page_size,
            items=members,
        )

    @exception_handler
    async def switch_current_user_group(
        self,
        request: Request,
        input: SwitchGroupRequest,
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> str:
        ctx = AppContext(
            trace_id=uuid4(), action=SWITCH_CURRENT_USER_GROUP, actor=credential.id
        )
        logger.info(
            msg=f"Starting Switch Current User Group Endpoint: {request.url}; params: ${input}",
            context=ctx,
        )
        await self.service.switch_current_user_active_group(
            input, ctx=ctx, credential=credential
        )
        logger.info(
            msg=f"Switch Current User Group Endpoint Finishes {request.url}; params: ${input};",
            context=ctx,
        )
        return "Success"

    @exception_handler
    async def invite_group_members(
        self,
        group_id: UUID = Path(..., description="Group id"),
        members: List[GroupMemberCreate] = Body(...),
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> BaseResponse[List[GroupInvitationInfo]]:
        ctx = AppContext(trace_id=uuid4(), action=INVITE_MEMBER, actor=credential.id)
        invitations = await self.service.invite_group_members(
            group_id=group_id,
            members=members,
            credential=credential,
            ctx=ctx,
        )
        return BaseResponse[List[GroupInvitationInfo]](
            data=invitations,
            message="Invitations created; invitation delivery is being processed",
            statusCode=201,
        )

    @exception_handler
    async def validate_group_invitation(
        self,
        invitation_id: UUID = Path(..., description="Invitation id"),
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> BaseResponse[str]:
        ctx = AppContext(
            trace_id=uuid4(),
            action=VALIDATE_GROUP_INVITATION,
            actor=credential.id,
        )
        result = await self.service.validate_group_invitation(
            invitation_id=invitation_id, credential=credential, ctx=ctx
        )
        return BaseResponse[str](
            data=result,
            message="Invitation accepted",
            statusCode=200,
        )
