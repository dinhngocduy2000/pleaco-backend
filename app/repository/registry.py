from typing import Callable
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.common.types import T
from app.external.redis.redis import RedisClient
from app.repository.group import GroupRepository
from app.repository.group_members import GroupMembersRepository
from app.repository.map import MapRepository
from app.repository.map_boundary import MapBoundaryRepository
from app.repository.map_tags import MapTagsRepository
from app.repository.group_invitations import GroupInvitationRepository
from app.repository.bot import BotRepository
from app.repository.robot_tags import RobotTagsRepository
from app.repository.tag import TagRepository
from app.repository.user import UserRepository


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
    _map_tags_repo: MapTagsRepository

    def __init__(self, pg_engine: AsyncEngine, redis_client: RedisClient) -> None:
        self._pg_engine = pg_engine
        self._user_repo = UserRepository(redis_client=redis_client)
        self._group_repo = GroupRepository(redis_client=redis_client)
        self._group_members_repo = GroupMembersRepository(redis_client=redis_client)
        self._group_invitation_repo = GroupInvitationRepository(redis_client=redis_client)
        self._bot_repo = BotRepository()
        self._robot_tags_repo = RobotTagsRepository()
        self._tag_repo = TagRepository()
        self._map_repo = MapRepository()
        self._map_boundary_repo = MapBoundaryRepository()
        self._map_tags_repo = MapTagsRepository()
        self._redis_client = redis_client

    async def transaction_wrapper(self, tx_func: Callable[[AsyncSession], T]) -> T:
        try:
            async_session = async_sessionmaker(self._pg_engine, expire_on_commit=False)
            session = async_session()
            await session.begin()
            res = await tx_func(session)
            await session.commit()
            return res
        except Exception as e:
            if session is not None and session.is_active:
                await session.rollback()
            raise e
        finally:
            if session is not None and session.is_active:
                await session.close()

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

    def map_tags_repo(self) -> MapTagsRepository:
        return self._map_tags_repo
