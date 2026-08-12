from typing import Generic, List, Optional
from uuid import UUID
from pydantic import BaseModel, Field

from app.common.types import T


class BaseResponse(BaseModel, Generic[T]):
    data: T = Field(..., description="Data")
    message: str = Field(..., description="Message")
    statusCode: int = Field(..., description="Status code")


class HashMapResponse(BaseModel):
    value: UUID = Field(..., description="ID")
    label: str = Field(..., description="Name")


class PaginationBaseRequest(BaseModel):
    page: Optional[int] = Field(None, description="Page number")
    page_size: Optional[int] = Field(None, description="Page size")


# Define a type variable (like <T> in TypeScript)


class PaginationBaseResponse(BaseModel, Generic[T]):
    """Generic pagination response

    Usage:
        PaginationBaseResponse[UserInfo]  # For users
        PaginationBaseResponse[GroupInfo]  # For groups
        PaginationBaseResponse[EventInfo]  # For events
    """

    total: int = Field(default=0, description="Total number of items")
    page: Optional[int] = Field(None, description="Page number")
    page_size: Optional[int] = Field(None, description="Page size")
    items: List[T] = Field(default=[], description="Items")
