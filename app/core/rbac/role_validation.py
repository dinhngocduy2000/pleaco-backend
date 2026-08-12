from functools import wraps
from typing import Optional
from uuid import UUID
from app.common.context import AppContext
from app.common.enum.user_roles import GroupRole
from app.common.exceptions import ForbiddenException
from app.common.middleware.logger import Logger
from app.core.rbac.permissions import (
    ROLE_HIERACHY,
    PermissionService,
)
from app.common.schemas.user import Credential

logger = Logger()


def require_permission(
    minimum_role: GroupRole, resouce_owner_id: Optional[UUID] = None
):
    def decorator(func):
        @wraps(func)
        async def wrapper(self, *arg, **kwargs):
            ctx: AppContext = kwargs.get("ctx")
            action = ctx.action
            credential: Credential = kwargs.get("credential")

            permission_service: PermissionService = self.permission_service
            group_member = await permission_service.get_group_member(
                credential=credential, ctx=ctx
            )
            if group_member is None:
                logger.error(
                    msg="This user is not found in the current group in role validation",
                    context=ctx,
                )
                raise ForbiddenException(
                    message="This user is not found in the current group"
                )

            if ROLE_HIERACHY.index(group_member.role) < ROLE_HIERACHY.index(
                minimum_role
            ):
                logger.error(
                    msg="This user doesn't have the sufficent role hierachy",
                    context=ctx,
                )
                raise ForbiddenException(
                    message="This user doesn't have the sufficent role"
                )

            if not permission_service.is_action_executable(
                role=minimum_role,
                action=action,
                is_owner=credential.id == resouce_owner_id,
            ):
                logger.error(msg="User's role doesnt allow this action")
                raise ForbiddenException(
                    message="This user is not permitted to perform this action"
                )

            return await func(self, *arg, **kwargs)

        return wrapper

    return decorator
