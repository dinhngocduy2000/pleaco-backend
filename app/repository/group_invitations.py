"""Redis-backed, expiring group invitation persistence."""

from uuid import UUID

from app.common.context import AppContext
from app.common.schemas.group import GroupInvitationInfo
from app.core.config import settings
from app.external.redis.redis import RedisClient


class GroupInvitationRepository:
    def __init__(self, redis_client: RedisClient) -> None:
        self._redis_client = redis_client

    @staticmethod
    def _key(invitation_id: UUID) -> str:
        return f"group-invitation:{invitation_id}"

    async def save(self, invitation: GroupInvitationInfo, ctx: AppContext) -> None:
        await self._redis_client.set(
            key=self._key(invitation.invitation_id),
            value=invitation.model_dump_json(),
            expire=settings.INVITATION_EXPIRE_SECONDS,
        )

    async def get(
        self, invitation_id: UUID, ctx: AppContext
    ) -> GroupInvitationInfo | None:
        value = await self._redis_client.get(self._key(invitation_id))
        if value is None:
            return None
        return GroupInvitationInfo.model_validate_json(value)

    async def delete(self, invitation_id: UUID, ctx: AppContext) -> None:
        await self._redis_client.delete(self._key(invitation_id))
        return
