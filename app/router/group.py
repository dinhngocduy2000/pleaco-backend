from typing import List
from fastapi import APIRouter, status
from app.common.schemas.common import BaseResponse, HashMapResponse, PaginationBaseResponse
from app.common.schemas.group import GroupInvitationInfo, GroupInfo, GroupMemberListInfo
from app.handler.group import GroupHandler


class GroupRouter:
    router: APIRouter
    handler: GroupHandler

    def __init__(self, handler: GroupHandler) -> None:
        self.router = APIRouter(prefix="", tags=["Groups"])
        self.handler = handler

        self.router.add_api_route(
            path="/create",
            endpoint=self.handler.create_group,
            methods=["POST"],
            response_model=BaseResponse[GroupInfo],
            status_code=status.HTTP_201_CREATED,
            summary="Create a new group",
            description="Create a new group with name, description, and members",
            response_description="The created group information",
            responses={
                201: {
                    "description": "Group created successfully",
                    "content": {
                        "application/json": {
                            "example": {
                                "id": "550e8400-e29b-41d4-a716-446655440000",
                                "name": "Group 1",
                                "description": "Group 1 description",
                                "members": [
                                    {
                                        "id": "550e8400-e29b-41d4-a716-446655440000",
                                        "name": "User 1",
                                        "email": "user1@example.com",
                                    }
                                ],
                            }
                        }
                    },
                },
                400: {
                    "description": "Bad request - Invalid input data",
                    "content": {
                        "application/json": {
                            "example": {"detail": "Invalid input data"}
                        }
                    },
                },
            },
        )

        self.router.add_api_route(
            path="/members",
            endpoint=self.handler.list_group_members,
            methods=["GET"],
            response_model=PaginationBaseResponse[GroupMemberListInfo],
            status_code=status.HTTP_200_OK,
            summary="List group members",
            description=(
                "List group members for group Owners and Admins.\n\n"
                "Example query options:\n"
                "```json\n"
                "{\n"
                '  "group_id": "550e8400-e29b-41d4-a716-446655440000",\n'
                '  "page": 1,\n'
                '  "page_size": 10,\n'
                '  "order_by": "joined_date",\n'
                '  "order_direction": "desc",\n'
                '  "email": "member@example.com",\n'
                '  "role": "member",\n'
                '  "status": "ACTIVE"\n'
                "}\n"
                "```\n\n"
                "Equivalent request: "
                "`/members?group_id=550e8400-e29b-41d4-a716-446655440000"
                "&page=1&page_size=10&order_by=joined_date&order_direction=desc"
                "&email=member%40example.com&role=member&status=ACTIVE`"
            ),
        )

        self.router.add_api_route(
            path="/{group_id}/members",
            endpoint=self.handler.invite_group_members,
            methods=["POST"],
            response_model=BaseResponse[List[GroupInvitationInfo]],
            status_code=status.HTTP_201_CREATED,
            summary="Invite members to a group",
            description="Add existing active or pending users and queue group invitation emails.",
        )

        self.router.add_api_route(
            path="/validation/{invitation_id}",
            endpoint=self.handler.validate_group_invitation,
            methods=["POST"],
            response_model=BaseResponse[str],
            status_code=status.HTTP_200_OK,
            summary="Accept a group invitation",
            description="Create group membership for the invited authenticated user.",
        )

        self.router.add_api_route(
            path="/key-value",
            endpoint=self.handler.list_group_key_value,
            methods=["GET"],
            response_model=BaseResponse[List[HashMapResponse]],
            status_code=status.HTTP_200_OK,
            summary="List all groups with their IDs and names",
            description="List all groups with their IDs and names",
            response_description="List of groups with their IDs and names",
        )

        self.router.add_api_route(
            path="/{group_id}",
            endpoint=self.handler.get_group,
            methods=["GET"],
            response_model=BaseResponse[GroupInfo],
            status_code=status.HTTP_200_OK,
            summary="Get a group by its ID",
            description="Get a group by its ID",
            response_description="The group information",
        )

        self.router.add_api_route(
            path="/switch",
            endpoint=self.handler.switch_current_user_group,
            methods=["PUT"],
            response_model=str,
            status_code=status.HTTP_200_OK,
            summary="Switch current user group",
            description="Switch current user group based on the group id",
            response_description="The switched group information",
        )
