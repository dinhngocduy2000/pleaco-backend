from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.context import AppContext
from app.common.enum.user_roles import GroupRole
from app.common.exceptions import BadRequestException
from app.common.schemas.tags import TagCreateDTO, TagInfo, TagListInfo
from app.common.schemas.user import Credential
from app.core.rbac.permissions import PermissionService
from app.core.rbac.role_validation import require_permission
from app.models.tag import Tag
from app.repository.registry import Registry


class TagService:
    def __init__(self, repo: Registry, permission_service: PermissionService) -> None:
        self.repo = repo
        self.permission_service = permission_service

    @require_permission(GroupRole.ADMIN)
    async def create_tag(
        self,
        tag_create: TagCreateDTO,
        group_id: UUID,
        credential: Credential,
        ctx: AppContext,
    ) -> TagInfo:
        async def _create_tag(session: AsyncSession) -> TagInfo:
            tag_repository = self.repo.tag_repo()
            existing_tag = await tag_repository.get_by_group_and_name(
                session=session,
                group_id=group_id,
                name=tag_create.name,
                ctx=ctx,
            )
            if existing_tag is not None:
                raise BadRequestException(
                    message="A tag with this name already exists in this group"
                )

            tag = await tag_repository.create_tag(
                session=session,
                group_id=group_id,
                name=tag_create.name,
                description=tag_create.description,
                color=tag_create.color,
                ctx=ctx,
            )
            return self._to_tag_info(tag)

        return await self.repo.transaction_wrapper(_create_tag)

    @require_permission(GroupRole.GUEST)
    async def list_tags(
        self,
        group_id: UUID,
        credential: Credential,
        ctx: AppContext,
    ) -> list[TagListInfo]:
        async def _list_tags(session: AsyncSession) -> list[TagListInfo]:
            tags = await self.repo.tag_repo().list_by_group(
                session=session, group_id=group_id, ctx=ctx
            )
            return [
                TagListInfo(id=tag.id, name=tag.name, color=tag.color)
                for tag in tags
            ]

        return await self.repo.transaction_wrapper(_list_tags)

    @staticmethod
    def _to_tag_info(tag: Tag) -> TagInfo:
        return TagInfo(
            id=tag.id,
            name=tag.name,
            color=tag.color,
            description=tag.description,
            created_at=tag.created_at,
            updated_at=tag.updated_at,
        )
