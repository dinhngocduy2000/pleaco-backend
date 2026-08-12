from datetime import date as Date, datetime
from typing import List, Optional
from uuid import UUID
from pydantic import BaseModel, Field

from app.common.enum.event_priority import EventCategory, EventPriority
from app.common.schemas.common import PaginationBaseRequest
from app.common.schemas.tags import TagInfo
from app.common.schemas.user import UserInfo


class EventCreate(BaseModel):
    name: str = Field("", description="Event's name")
    destination: str = Field("", description="Event's destination")
    cost: str = Field("", description="Event's cost")
    start_time: datetime = Field("", description="Event's start time")
    end_time: datetime = Field("", description="Event's end time")
    priority: EventPriority = Field("", description="Event's priority")
    category: EventCategory = Field("", description="Event's category")
    description: str = Field("", description="Event's description")
    tags: Optional[List[UUID]] = Field(default=None, description="Event's tags")
    group_id: UUID = Field("", description="Event's group id")


class EventCreateDomain(EventCreate):
    owner_id: UUID = Field("", description="Event's owner id")
    id: Optional[UUID] = Field("", description="Event's id")


class EventUpdate(BaseModel):
    name: Optional[str] = Field(None, description="Event's name")
    destination: Optional[str] = Field(None, description="Event's destination")
    cost: Optional[str] = Field(None, description="Event's cost")
    start_time: Optional[datetime] = Field(None, description="Event's start time")
    end_time: Optional[datetime] = Field(None, description="Event's end time")
    priority: Optional[EventPriority] = Field(None, description="Event's priority")
    category: Optional[EventCategory] = Field(None, description="Event's category")
    description: Optional[str] = Field(None, description="Event's description")
    tags: Optional[List[UUID]] = Field(default=None, description="Event's tags")


class EventListInfo(BaseModel):
    id: UUID = Field(None, description="Event's id")
    name: str = Field(None, description="Event's name")
    start_time: datetime = Field(None, description="Event's start time")
    end_time: datetime = Field(None, description="Event's end time")
    priority: EventPriority = Field(None, description="Event's priority")
    category: EventCategory = Field(None, description="Event's category")


class EventDetailInfo(BaseModel):
    id: UUID = Field(None, description="Event's id")
    name: str = Field(None, description="Event's name")
    destination: str = Field(None, description="Event's destination")
    cost: str = Field(None, description="Event's cost")
    start_time: datetime = Field(None, description="Event's start time")
    end_time: datetime = Field(None, description="Event's end time")
    priority: EventPriority = Field(None, description="Event's priority")
    category: EventCategory = Field(None, description="Event's category")
    description: str = Field(None, description="Event's description")
    tags: Optional[List[TagInfo]] = Field(default=None, description="Event's tags")
    created_at: datetime = Field(None, description="Event's created at")
    updated_at: datetime = Field(None, description="Event's updated at")
    group_id: UUID = Field(None, description="Event's group id")
    owner_id: UUID = Field(None, description="Event's owner id")
    owner: Optional[UserInfo] = Field(default=None, description="Event's owner")


class EventQuery(PaginationBaseRequest):
    id: Optional[UUID] = Field(default=None, description="Event's id")
    group_id: Optional[UUID] = Field(default=None, description="Event's group id")
    name: Optional[str] = Field(default=None, description="Event's name")
    cost: Optional[str] = Field(default=None, description="Event's cost")
    start_time: Optional[datetime] = Field(
        default=None, description="Event's start time"
    )
    end_time: Optional[datetime] = Field(default=None, description="Event's end time")
    priority: Optional[EventPriority] = Field(
        default=None, description="Event's priority"
    )
    category: Optional[EventCategory] = Field(
        default=None, description="Event's category"
    )
    owner_id: Optional[UUID] = Field(default=None, description="Event's owner id")
    tags: Optional[List[UUID]] = Field(default=None, description="Event's tags")


class ListEventQuery(BaseModel):
    month: int = Field(None, description="List event's month")
    year: int = Field(None, description="List event's year")
    group_id: UUID = Field(None, description="List event's group id")
    owner_id: Optional[UUID] = Field(None, description="List event's owner id")
    tags: Optional[List[UUID]] = Field(default=None, description="List event's tags")


class EventCalendarView(BaseModel):
    date: Date = Field(None, description="Calendar view's date")
    events: List[EventListInfo] = Field(None, description="Calendar view's events")


class EventJoinOptions(BaseModel):
    include_tags: Optional[bool] = Field(
        default=False, description="Include tags in the response"
    )
    include_owner: Optional[bool] = Field(
        default=False, description="Include owner in the response"
    )


class EventTagCreate(BaseModel):
    event_id: UUID = Field(None, description="Event's id")
    tag_id: UUID = Field(None, description="Tag's id")
