from typing import Dict, List
from uuid import UUID, uuid4
from fastapi import Body, Depends, Path, Request, Response
from app.common.context import AppContext
from app.common.enum.context_actions import (
    CREATE_GROUP,
    EDIT_MEMBER,
    GET_GROUP_BY_ID,
    GET_GROUP_INVITATION,
    INVITE_MEMBER,
    LIST_GROUP_MEMBERS,
    LIST_GROUP_KEY_VALUE,
    REMOVE_MEMBER,
    SWITCH_CURRENT_USER_GROUP,
    VALIDATE_GROUP_INVITATION,
)
from app.common.exceptions import UnauthorizedException
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
    GroupMemberInfo,
    GroupMemberListInfo,
    GroupMemberListQuery,
    GroupMemberUpdate,
)
from app.common.schemas.user import Credential, SwitchGroupRequest
from app.services.auth import AuthService
from app.services.group import GroupService

logger = Logger()


class GroupHandler:
    service: GroupService
    auth_service: AuthService

    def __init__(self, service: GroupService, auth_service: AuthService) -> None:
        self.service = service
        self.auth_service = auth_service

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
        response: Response,
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
        access_token = request.cookies.get("access_token")
        if access_token is None:
            raise UnauthorizedException("Unauthorized")
        replacement_token, remaining_ttl = (
            await self.auth_service.rotate_access_token_active_group(
                access_token=access_token,
                active_group_id=input.group_id,
                ctx=ctx,
            )
        )
        response.set_cookie(
            key="access_token",
            value=replacement_token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=remaining_ttl,
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
    async def update_group_member(
        self,
        group_id: UUID = Path(..., description="Group id"),
        member_id: UUID = Path(..., description="Group member id"),
        member_update: GroupMemberUpdate = Body(...),
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> BaseResponse[GroupMemberInfo]:
        ctx = AppContext(trace_id=uuid4(), action=EDIT_MEMBER, actor=credential.id)
        member = await self.service.update_group_member(
            group_id=group_id,
            member_id=member_id,
            member_update=member_update,
            credential=credential,
            ctx=ctx,
        )
        return BaseResponse[GroupMemberInfo](
            data=member, message="Group member updated", statusCode=200
        )

    @exception_handler
    async def delete_group_member(
        self,
        group_id: UUID = Path(..., description="Group id"),
        member_id: UUID = Path(..., description="Group member id"),
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> None:
        ctx = AppContext(trace_id=uuid4(), action=REMOVE_MEMBER, actor=credential.id)
        await self.service.delete_group_member(
            group_id=group_id,
            member_id=member_id,
            credential=credential,
            ctx=ctx,
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

    @exception_handler
    async def get_group_invitation(
        self,
        invitation_id: UUID = Path(..., description="Invitation id"),
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> BaseResponse[GroupInvitationInfo]:
        ctx = AppContext(
            trace_id=uuid4(),
            action=GET_GROUP_INVITATION,
            actor=credential.id,
        )
        invitation = await self.service.get_group_invitation(
            invitation_id=invitation_id,
            credential=credential,
            ctx=ctx,
        )
        return BaseResponse[GroupInvitationInfo](
            data=invitation,
            message="Success",
            statusCode=200,
        )
