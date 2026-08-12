from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, List, Optional
from uuid import UUID
from pydantic import BaseModel, Field

from app.common.enum.user_roles import GroupRole

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


class GroupMemberCreate(BaseModel):
    member_id: UUID = Field(None, description="Group member's id")
    group_id: UUID = Field(None, description="Group's id")
