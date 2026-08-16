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

    @staticmethod
    def _pending_key(invitation: GroupInvitationInfo) -> str:
        return (
            f"group-invitation:pending:{invitation.group_id}:{invitation.member_id}"
        )

    async def save(self, invitation: GroupInvitationInfo, ctx: AppContext) -> None:
        await self._redis_client.set(
            key=self._key(invitation.invitation_id),
            value=invitation.model_dump_json(),
            expire=settings.INVITATION_EXPIRE_SECONDS,
        )

    async def replace_pending_invitation(
        self, invitation: GroupInvitationInfo, ctx: AppContext
    ) -> None:
        """Atomically replace any unexpired invitation for the same group member."""
        await self._redis_client.eval(
            """
            local old_invitation_id = redis.call('GET', KEYS[2])
            if old_invitation_id then
                redis.call('DEL', ARGV[4] .. old_invitation_id)
            end
            redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
            redis.call('SET', KEYS[2], ARGV[3], 'EX', ARGV[2])
            """,
            2,
            self._key(invitation.invitation_id),
            self._pending_key(invitation),
            invitation.model_dump_json(),
            settings.INVITATION_EXPIRE_SECONDS,
            str(invitation.invitation_id),
            "group-invitation:",
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

    async def consume(self, invitation: GroupInvitationInfo, ctx: AppContext) -> None:
        """Delete an accepted invitation without removing a newer replacement index."""
        await self._redis_client.eval(
            """
            if redis.call('GET', KEYS[2]) == ARGV[1] then
                redis.call('DEL', KEYS[2])
            end
            redis.call('DEL', KEYS[1])
            """,
            2,
            self._key(invitation.invitation_id),
            self._pending_key(invitation),
            str(invitation.invitation_id),
        )
