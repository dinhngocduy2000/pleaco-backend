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
    BotListInfo,
    BotListQuery,
)
from app.common.schemas.tags import TagInfo
from app.common.schemas.user import Credential
from app.core.rbac.permissions import PermissionService
from app.core.rbac.role_validation import require_permission
from app.models.robot import Robot
from app.repository.registry import Registry


class BotService:
    def __init__(self, repo: Registry, permission_service: PermissionService) -> None:
        self.repo = repo
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
            bot_repository = self.repo.bot_repo()
            tag_repository = self.repo.tag_repo()
            existing_bot = await bot_repository.get_by_group_and_serial(
                session=session,
                group_id=group_id,
                serial_num=bot_create.serial_num,
                ctx=ctx,
            )
            if existing_bot is not None:
                raise BadRequestException(
                    message="A bot with this serial number already exists in this group"
                )

            tags = await tag_repository.get_by_ids_and_group(
                session=session,
                tag_ids=bot_create.tags,
                group_id=group_id,
                ctx=ctx,
            )
            if len(tags) != len(bot_create.tags):
                raise NotFoundException(message="One or more tags were not found")

            bot = await bot_repository.create_bot(
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

        return await self.repo.transaction_wrapper(_create_bot)

    @require_permission(GroupRole.GUEST)
    async def list_bots(
        self,
        query: BotListQuery,
        group_id: UUID,
        credential: Credential,
        ctx: AppContext,
    ) -> tuple[list[BotListInfo], int]:
        async def _list_bots(session: AsyncSession) -> tuple[list[BotListInfo], int]:
            rows, total = await self.repo.bot_repo().list_bots(
                session=session, query=query, ctx=ctx
            )
            tags_by_robot = await self.repo.robot_tags_repo().get_by_robot_ids(
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

        return await self.repo.transaction_wrapper(_list_bots)

    @require_permission(GroupRole.MODERATOR)
    async def delete_bot(
        self,
        bot_id: UUID,
        credential: Credential,
        group_id: str,
        ctx: AppContext,
    ) -> None:
        if group_id is None:
            raise ForbiddenException(message="A group must be selected")

        async def _delete_bot(session: AsyncSession) -> None:
            bot_repository = self.repo.bot_repo()
            bot = await bot_repository.get_by_id_and_group_for_update(
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

            deleted_bot = await bot_repository.hard_delete_bot(
                session=session,
                bot_id=bot_id,
                group_id=group_id,
                ctx=ctx,
            )
            if deleted_bot is None:
                raise NotFoundException(message="Bot not found in the current group")

        await self.repo.transaction_wrapper(_delete_bot)

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
