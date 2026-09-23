from collections.abc import Sequence
from typing import Any, Protocol, TypeVar
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.context import AppContext
from app.common.enum.robot import RobotOperationalStatus
from app.common.enum.user_roles import GroupRole
from app.common.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
)
from app.common.schemas.bot import (
    BotCreateDTO,
    BotCreateDomain,
    BotInfo,
    BotKeyValueInfo,
    BotListInfo,
    BotListQuery,
)
from app.common.schemas.group import GroupMemberInfo
from app.common.schemas.tags import TagInfo
from app.common.schemas.user import Credential
from app.core.rbac.role_validation import require_permission
from app.models.robot import Robot
from app.models.tag import Tag
from app.repository.registry import TransactionRunner

T = TypeVar("T")


class PermissionChecker(Protocol):
    """Permission behavior required by the service authorization decorator."""

    async def get_group_member(
        self,
        credential: Credential,
        ctx: AppContext,
        group_id: UUID | None = None,
    ) -> GroupMemberInfo | None: ...

    @staticmethod
    def is_action_executable(
        role: GroupRole,
        action: str,
        is_owner: bool = False,
    ) -> bool: ...


class BotRepositoryPort(Protocol):
    """Persistence operations consumed by ``BotService``."""

    async def get_by_group_and_serial(
        self,
        session: AsyncSession,
        group_id: UUID,
        serial_num: str,
        ctx: AppContext,
    ) -> Robot | None: ...

    async def create_bot(
        self,
        session: AsyncSession,
        bot_create: BotCreateDomain,
        tags: Sequence[Tag],
        ctx: AppContext,
    ) -> Robot: ...

    async def list_bots(
        self,
        session: AsyncSession,
        query: BotListQuery,
        ctx: AppContext,
    ) -> tuple[list[dict[str, Any]], int]: ...

    async def list_bot_key_value(
        self,
        session: AsyncSession,
        group_id: UUID,
        search: str | None,
        ctx: AppContext,
    ) -> list[dict[str, Any]]: ...

    async def get_by_id_and_group_for_update(
        self,
        session: AsyncSession,
        bot_id: UUID,
        group_id: UUID,
        ctx: AppContext,
    ) -> Robot | None: ...

    async def hard_delete_bot(
        self,
        session: AsyncSession,
        bot_id: UUID,
        group_id: UUID,
        ctx: AppContext,
    ) -> Robot | None: ...


class TagRepositoryPort(Protocol):
    """Tag lookups required while provisioning a bot."""

    async def get_by_ids_and_group(
        self,
        session: AsyncSession,
        tag_ids: list[UUID],
        group_id: UUID,
        ctx: AppContext,
    ) -> list[Tag]: ...


class RobotTagsRepositoryPort(Protocol):
    """Tag lookups required while building bot list responses."""

    async def get_by_robot_ids(
        self,
        session: AsyncSession,
        robot_ids: list[UUID],
        group_id: UUID,
        ctx: AppContext,
    ) -> dict[UUID, list[Tag]]: ...


class BotService:
    def __init__(
        self,
        bot_repository: BotRepositoryPort,
        tag_repository: TagRepositoryPort,
        robot_tags_repository: RobotTagsRepositoryPort,
        transactions: TransactionRunner,
        permission_service: PermissionChecker,
    ) -> None:
        self.bot_repository = bot_repository
        self.tag_repository = tag_repository
        self.robot_tags_repository = robot_tags_repository
        self.transactions = transactions
        self.permission_service = permission_service

    @require_permission(GroupRole.MODERATOR)
    async def create_bot(
        self,
        bot_create: BotCreateDTO,
        group_id: UUID,
        credential: Credential,
        ctx: AppContext,
    ) -> BotInfo:
        async def _create_bot(session: AsyncSession) -> BotInfo:
            existing_bot = await self.bot_repository.get_by_group_and_serial(
                session=session,
                group_id=group_id,
                serial_num=bot_create.serial_num,
                ctx=ctx,
            )
            if existing_bot is not None:
                raise BadRequestException(
                    message="A bot with this serial number already exists in this group"
                )

            tags = await self.tag_repository.get_by_ids_and_group(
                session=session,
                tag_ids=bot_create.tags,
                group_id=group_id,
                ctx=ctx,
            )
            if len(tags) != len(bot_create.tags):
                raise NotFoundException(message="One or more tags were not found")

            bot = await self.bot_repository.create_bot(
                session=session,
                bot_create=BotCreateDomain(
                    group_id=group_id,
                    name=bot_create.name,
                    serial_num=bot_create.serial_num,
                    model=bot_create.model,
                    ip_address=(
                        str(bot_create.ip_address)
                        if bot_create.ip_address is not None
                        else None
                    ),
                    tag_ids=bot_create.tags,
                ),
                tags=tags,
                ctx=ctx,
            )
            return self._to_bot_info(bot)

        return await self.transactions.transaction_wrapper(_create_bot)

    @require_permission(GroupRole.GUEST)
    async def list_bots(
        self,
        query: BotListQuery,
        group_id: UUID,
        credential: Credential,
        ctx: AppContext,
    ) -> tuple[list[BotListInfo], int]:
        async def _list_bots(session: AsyncSession) -> tuple[list[BotListInfo], int]:
            rows, total = await self.bot_repository.list_bots(
                session=session, query=query, ctx=ctx
            )
            tags_by_robot = await self.robot_tags_repository.get_by_robot_ids(
                session=session,
                robot_ids=[row["id"] for row in rows],
                group_id=group_id,
                ctx=ctx,
            )
            return [
                BotListInfo.model_validate(
                    {
                        **row,
                        "ip_address": (
                            str(row["ip_address"])
                            if row["ip_address"] is not None
                            else None
                        ),
                        "tags": [
                            TagInfo(
                                id=tag.id,
                                name=tag.name,
                                color=tag.color,
                                description=tag.description,
                                created_at=tag.created_at,
                                updated_at=tag.updated_at,
                            )
                            for tag in tags_by_robot.get(row["id"], [])
                        ],
                    }
                )
                for row in rows
            ], total

        return await self.transactions.transaction_wrapper(_list_bots)

    @require_permission(GroupRole.GUEST)
    async def list_bot_key_value(
        self,
        search: str | None,
        group_id: UUID | None,
        credential: Credential,
        ctx: AppContext,
    ) -> list[BotKeyValueInfo]:
        """Return searchable bot ID/name/serial pairs for the caller's active group."""
        if group_id is None:
            raise ForbiddenException(message="A group must be selected")

        async def _list_bot_key_value(session: AsyncSession) -> list[BotKeyValueInfo]:
            rows = await self.bot_repository.list_bot_key_value(
                session=session,
                group_id=group_id,
                search=search,
                ctx=ctx,
            )
            return [
                BotKeyValueInfo(
                    value=row["id"],
                    label=row["name"],
                    serial_num=row["serial_num"],
                )
                for row in rows
            ]

        return await self.transactions.transaction_wrapper(_list_bot_key_value)

    @require_permission(GroupRole.MODERATOR)
    async def delete_bot(
        self,
        bot_id: UUID,
        credential: Credential,
        group_id: UUID | None,
        ctx: AppContext,
    ) -> None:
        if group_id is None:
            raise ForbiddenException(message="A group must be selected")

        async def _delete_bot(session: AsyncSession) -> None:
            bot = await self.bot_repository.get_by_id_and_group_for_update(
                session=session,
                bot_id=bot_id,
                group_id=group_id,
                ctx=ctx,
            )
            if bot is None:
                raise NotFoundException(message="Bot not found")
            if bot.operational_status == RobotOperationalStatus.EXECUTING:
                raise BadRequestException(
                    message=(
                        "Cannot delete a bot while it is executing; "
                        "stop the operation or wait for it to finish"
                    )
                )

            deleted_bot = await self.bot_repository.hard_delete_bot(
                session=session,
                bot_id=bot_id,
                group_id=group_id,
                ctx=ctx,
            )
            if deleted_bot is None:
                raise NotFoundException(message="Bot not found in the current group")

        await self.transactions.transaction_wrapper(_delete_bot)

    @staticmethod
    def _to_bot_info(bot: Robot) -> BotInfo:
        return BotInfo(
            id=bot.id,
            group_id=bot.group_id,
            map_id=bot.map_id,
            name=bot.name,
            serial_num=bot.serial_num,
            model=bot.model,
            ip_address=str(bot.ip_address) if bot.ip_address is not None else None,
            connection_status=bot.connection_status,
            operational_status=bot.operational_status,
            last_seen_at=bot.last_seen_at,
            tags=[
                TagInfo(
                    id=tag.id,
                    name=tag.name,
                    color=tag.color,
                    description=tag.description,
                    created_at=tag.created_at,
                    updated_at=tag.updated_at,
                )
                for tag in bot.tags
            ],
            created_at=bot.created_at,
            updated_at=bot.updated_at,
        )
