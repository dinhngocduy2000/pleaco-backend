from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.context import AppContext
from app.common.enum.user_roles import GroupRole
from app.common.exceptions import BadRequestException, NotFoundException
from app.common.schemas.bot import BotCreateDTO, BotCreateDomain, BotInfo
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

            tags = await bot_repository.get_tags_by_ids(
                session=session, tag_ids=bot_create.tags, ctx=ctx
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
