from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Enum, ForeignKey, func
from app.common.enum.group_member_status import GroupMemberInvitationStatus
from app.common.enum.user_roles import GroupRole
from app.core.database import Base
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID as PostgreSQL_UUID


class GroupMembers(Base):
    __tablename__ = "group_members"

    member_id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    group_id: Mapped[UUID] = mapped_column(
        PostgreSQL_UUID(as_uuid=True),
        ForeignKey("groups.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    invitation_status: Mapped[GroupMemberInvitationStatus] = mapped_column(
        Enum(
            GroupMemberInvitationStatus,
            name="group_member_status",
            values_callable=lambda statuses: [status.value for status in statuses],
        ),
        nullable=False,
        default=GroupMemberInvitationStatus.ACCEPTED,
        server_default=GroupMemberInvitationStatus.ACCEPTED.value,
    )
    invitation_id: Mapped[UUID | None] = mapped_column(
        PostgreSQL_UUID(as_uuid=True), nullable=True
    )
    invitation_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    role: Mapped[GroupRole] = mapped_column(
        Enum(GroupRole, values_callable=lambda roles: [role.value for role in roles]),
        nullable=False,
        default=GroupRole.MEMBER,
        server_default=GroupRole.MEMBER.value,
    )

    user: Mapped["User"] = relationship("User", back_populates="group_members")  # type: ignore
    group: Mapped["Group"] = relationship("Group", back_populates="members")  # type: ignore
