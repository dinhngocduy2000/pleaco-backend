from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, List, Optional
from uuid import UUID
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.schemas.common import PaginationBaseRequest

if TYPE_CHECKING:
    from app.common.schemas.user import UserInfo


class GroupJoinOption(BaseModel):
    include_members: Optional[bool] = False


class GroupInfo(BaseModel):
    id: UUID = Field(None, description="Group's id")
    name: str = Field(None, description="Group's name")
    created_at: datetime = Field(None, description="Group's created at")
    updated_at: datetime = Field(None, description="Group's updated at")
    members: Optional[List[UserInfo]] = Field(None, description="Group's members")


class GroupCreateDTO(BaseModel):
    name: str = Field(None, description="Group's name")
    description: Optional[str] = Field(None, description="Group's description")
    members: Optional[List[UUID]] = Field(default=[], description="Group's members")


class GroupCreateDomain(GroupCreateDTO):
    owner_id: UUID = Field(None, description="Group's owner id")


class GroupQuery(BaseModel):
    name: Optional[str] = Field(None, description="Group's name")
    id: Optional[UUID] = Field(None, description="Group's id")
    owner_id: Optional[UUID] = Field(None, description="Group's owner id")
    members: Optional[List[UUID]] = Field(default=[], description="Group's members")


class GroupUpdate(BaseModel):
    name: Optional[str] = Field(None, description="Group's name")
    description: Optional[str] = Field(None, description="Group's description")
    members: Optional[List[UUID]] = Field(default=[], description="Group's members")


class GroupMemberInfo(BaseModel):
    member_id: UUID = Field(None, description="Group member's id")
    group_id: UUID = Field(None, description="Group's id")
    created_at: datetime = Field(None, description="Group member's created at")
    updated_at: datetime = Field(None, description="Group member's updated at")
    role: GroupRole = Field("", description="Group member's role")


class GroupMemberOrderBy(str, Enum):
    NAME = "name"
    JOINED_DATE = "joined_date"


class GroupMemberOrderDirection(str, Enum):
    ASC = "asc"
    DESC = "desc"


class GroupMemberListQuery(PaginationBaseRequest):
    group_id: UUID = Field(..., description="Group id")
    order_by: GroupMemberOrderBy = Field(
        GroupMemberOrderBy.JOINED_DATE, description="Field to order by"
    )
    order_direction: GroupMemberOrderDirection = Field(
        GroupMemberOrderDirection.DESC, description="Ordering direction"
    )
    email: Optional[str] = Field(None, description="Email filter")
    role: Optional[GroupRole] = Field(None, description="Group role filter")
    status: Optional[UserStatus] = Field(None, description="User status filter")


class GroupMemberListInfo(BaseModel):
    member_id: UUID = Field(..., description="Member id")
    image_url: Optional[str] = Field(None, description="Member image url")
    email: str = Field(..., description="Member email")
    name: str = Field(..., description="Member name")
    joined_at: datetime = Field(..., description="Date the member joined the group")
    role: GroupRole = Field(..., description="Member role in the group")
    status: UserStatus = Field(..., description="Member account status")


class GroupMemberCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: EmailStr = Field(..., description="Existing member's email")
    role: GroupRole = Field(..., description="Role assigned in the group")


class GroupInvitationInfo(BaseModel):
    invitation_id: UUID = Field(..., description="Invitation id")
    group_id: UUID = Field(..., description="Group id")
    member_id: UUID = Field(..., description="Invited member id")
    email: EmailStr = Field(..., description="Invited member email")
    role: GroupRole = Field(..., description="Invited group role")
    group_name: str = Field(..., description="Group name")
    invited_by: UUID = Field(..., description="User who sent the invitation")
    created_at: datetime = Field(..., description="Invitation creation time")
    expires_at: datetime = Field(..., description="Invitation expiration time")
