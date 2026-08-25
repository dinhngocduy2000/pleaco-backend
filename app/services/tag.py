from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.context import AppContext
from app.common.enum.user_roles import GroupRole
from app.common.schemas.tags import TagListInfo
from app.common.schemas.user import Credential
from app.core.rbac.permissions import PermissionService
from app.core.rbac.role_validation import require_permission
from app.repository.registry import Registry


class TagService:
    def __init__(self, repo: Registry, permission_service: PermissionService) -> None:
        self.repo = repo
        self.permission_service = permission_service

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
