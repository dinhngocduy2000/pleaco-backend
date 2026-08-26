from datetime import datetime
from typing import Generic, List, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.common.types import T


class BaseResponse(BaseModel, Generic[T]):
    data: T = Field(..., description="Data")
    message: str = Field(..., description="Message")
    statusCode: int = Field(..., description="Status code")


class HashMapResponse(BaseModel):
    value: UUID = Field(..., description="ID")
    label: str = Field(..., description="Name")


class PaginationBaseRequest(BaseModel):
    page: int = Field(1, ge=1, description="Page number")
    page_size: int = Field(10, ge=1, le=100, description="Page size")


# Define a type variable (like <T> in TypeScript)


class PaginationBaseResponse(BaseModel, Generic[T]):
    """Generic pagination response

    Usage:
        PaginationBaseResponse[UserInfo]  # For users
        PaginationBaseResponse[GroupInfo]  # For groups
        PaginationBaseResponse[EventInfo]  # For events
    """

    total: int = Field(default=0, description="Total number of items")
    page: int = Field(1, description="Page number")
    page_size: int = Field(10, description="Page size")
    items: List[T] = Field(default=[], description="Items")


class CursorPayload(BaseModel):
    """Opaque cursor contents shared by timestamp-and-UUID keyset pagination."""

    model_config = ConfigDict(extra="forbid")

    v: Literal[1] = Field(1, description="Cursor format version")
    created_at: datetime = Field(..., description="Cursor creation timestamp")
    id: UUID = Field(..., description="Cursor UUID tie-breaker")

    @field_validator("created_at")
    @classmethod
    def created_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Cursor timestamp must include a timezone")
        return value


class CursorPaginationRequest(BaseModel):
    """Shared bidirectional cursor pagination query parameters."""

    after: str | None = Field(
        None,
        min_length=1,
        max_length=2048,
        description="Opaque cursor for the next page",
    )
    before: str | None = Field(
        None,
        min_length=1,
        max_length=2048,
        description="Opaque cursor for the previous page",
    )
    limit: int = Field(10, ge=1, le=100, description="Maximum items to return")

    @model_validator(mode="after")
    def cursors_are_mutually_exclusive(self) -> Self:
        if self.after is not None and self.before is not None:
            raise ValueError("after and before cursors are mutually exclusive")
        return self


class CursorPaginationMetadata(BaseModel):
    """Navigation metadata for a cursor-paginated collection."""

    limit: int = Field(..., description="Maximum items requested")
    next_cursor: str | None = Field(..., description="Cursor for the next page")
    previous_cursor: str | None = Field(
        ..., description="Cursor for the previous page"
    )
    has_next: bool = Field(..., description="Whether a next page is available")
    has_previous: bool = Field(
        ..., description="Whether a previous page is available"
    )


class CursorPaginationResponse(CursorPaginationMetadata, Generic[T]):
    """Generic bidirectional cursor pagination response."""

    items: List[T] = Field(..., description="Items")
