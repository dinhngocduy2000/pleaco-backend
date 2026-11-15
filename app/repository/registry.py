from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.common.types import T
from app.common.exceptions import ForbiddenException
from app.external.redis.redis import RedisClient
from app.repository.group import GroupRepository
from app.repository.docking_station import DockingStationRepository
from app.repository.environment_zone import EnvironmentZoneRepository
from app.repository.group_members import GroupMembersRepository
from app.repository.map import MapRepository
from app.repository.map_boundary import MapBoundaryRepository
from app.repository.map_tags import MapTagsRepository
from app.repository.group_invitations import GroupInvitationRepository
from app.repository.bot import BotRepository
from app.repository.robot_tags import RobotTagsRepository
from app.repository.tag import TagRepository
from app.repository.user import UserRepository
from app.repository.robot_action import RobotActionRepository


@dataclass(frozen=True)
class DbAccessContext:
    actor_id: UUID | None
    group_id: UUID | None


class TransactionRunner(Protocol):
    """Run one service operation inside the application's transaction boundary."""

    async def transaction_wrapper(
        self,
        tx_func: Callable[[AsyncSession], Awaitable[T]],
        context: DbAccessContext,
    ) -> T: ...


class Registry:
    _redis_client: RedisClient
    _pg_engine: AsyncEngine
    _user_repo: UserRepository
    _group_repo: GroupRepository
    _group_members_repo: GroupMembersRepository
    _group_invitation_repo: GroupInvitationRepository
    _bot_repo: BotRepository
    _robot_tags_repo: RobotTagsRepository
    _tag_repo: TagRepository
    _map_repo: MapRepository
    _map_boundary_repo: MapBoundaryRepository
    _environment_zone_repo: EnvironmentZoneRepository
    _docking_station_repo: DockingStationRepository
    _map_tags_repo: MapTagsRepository

    def robot_action_repo(self) -> RobotActionRepository:
        return RobotActionRepository()

    def __init__(self, pg_engine: AsyncEngine, redis_client: RedisClient) -> None:
        self._pg_engine = pg_engine
        self._user_repo = UserRepository(redis_client=redis_client)
        self._group_repo = GroupRepository(redis_client=redis_client)
        self._group_members_repo = GroupMembersRepository(redis_client=redis_client)
        self._group_invitation_repo = GroupInvitationRepository(
            redis_client=redis_client
        )
        self._bot_repo = BotRepository()
        self._robot_tags_repo = RobotTagsRepository()
        self._tag_repo = TagRepository()
        self._map_repo = MapRepository()
        self._map_boundary_repo = MapBoundaryRepository()
        self._environment_zone_repo = EnvironmentZoneRepository()
        self._docking_station_repo = DockingStationRepository()
        self._map_tags_repo = MapTagsRepository()
        self._redis_client = redis_client

    async def transaction_wrapper(
        self, tx_func: Callable[[AsyncSession], Awaitable[T]], context: DbAccessContext
    ) -> T:
        async_session = async_sessionmaker(self._pg_engine, expire_on_commit=False)
        async with async_session() as session:
            async with session.begin():
                await self.set_transaction_context(session, context)
                if context.actor_id is not None and context.group_id is not None:
                    member = await session.scalar(
                        text(
                            "SELECT 1 FROM public.group_members "
                            "WHERE member_id = :actor_id AND group_id = :group_id "
                            "AND invitation_status = 'ACCEPTED' FOR SHARE"
                        ),
                        {"actor_id": context.actor_id, "group_id": context.group_id},
                    )
                    if member is None:
                        raise ForbiddenException(
                            message="This user is not found in the current group"
                        )
                return await tx_func(session)

    @staticmethod
    async def set_transaction_context(
        session: AsyncSession, context: DbAccessContext
    ) -> None:
        """Set tenant identity for this transaction only, including absent values."""
        await session.execute(
            text("SELECT set_config('app.actor_id', :actor_id, true)"),
            {"actor_id": str(context.actor_id) if context.actor_id else ""},
        )
        await session.execute(
            text("SELECT set_config('app.group_id', :group_id, true)"),
            {"group_id": str(context.group_id) if context.group_id else ""},
        )

    async def resolve_robot_group(self, robot_id: UUID) -> UUID | None:
        """Resolve a broker robot UUID using the narrowly scoped database helper."""
        async with self._pg_engine.connect() as connection:
            return await connection.scalar(
                text("SELECT public.pleaco_robot_group(:robot_id)"),
                {"robot_id": robot_id},
            )

    def user_repo(self) -> UserRepository:
        return self._user_repo

    def group_repo(self) -> GroupRepository:
        return self._group_repo

    def group_members_repo(self) -> GroupMembersRepository:
        return self._group_members_repo

    def group_invitation_repo(self) -> GroupInvitationRepository:
        return self._group_invitation_repo

    def bot_repo(self) -> BotRepository:
        return self._bot_repo

    def robot_tags_repo(self) -> RobotTagsRepository:
        return self._robot_tags_repo

    def tag_repo(self) -> TagRepository:
        return self._tag_repo

    def map_repo(self) -> MapRepository:
        return self._map_repo

    def map_boundary_repo(self) -> MapBoundaryRepository:
        return self._map_boundary_repo

    def environment_zone_repo(self) -> EnvironmentZoneRepository:
        return self._environment_zone_repo

    def map_tags_repo(self) -> MapTagsRepository:
        return self._map_tags_repo

    def docking_station_repo(self) -> DockingStationRepository:
        return self._docking_station_repo
