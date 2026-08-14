from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.common.context import AppContext
from app.common.enum.user_status import UserStatus
from app.common.exceptions import UnauthorizedException
from app.common.schemas.user import UserInfo, UserQuery
from app.models.user import User
from app.services.auth import AuthService


def _ctx() -> AppContext:
    return AppContext(trace_id=uuid4(), action="TEST_CURRENT_USER_PROFILE")


class UserRepositoryStub:
    def __init__(self, cached_profile: UserInfo | None, user: User | None) -> None:
        self.cached_profile = cached_profile
        self.user = user
        self.queried_user_id = None
        self.cached_after_miss: UserInfo | None = None

    async def get_cached_user_profile(self, user_id, ctx) -> UserInfo | None:
        return self.cached_profile

    async def get(self, *, query: UserQuery, **kwargs) -> User | None:
        self.queried_user_id = query.id
        return self.user

    async def set_cached_user_profile(self, user_profile: UserInfo, ctx) -> None:
        self.cached_after_miss = user_profile


class RegistryStub:
    def __init__(self, user_repo: UserRepositoryStub) -> None:
        self._user_repo = user_repo
        self.transaction_calls = 0

    def user_repo(self) -> UserRepositoryStub:
        return self._user_repo

    async def transaction_wrapper(self, callback):
        self.transaction_calls += 1
        return await callback(SimpleNamespace())


def _service(repo: RegistryStub) -> AuthService:
    return AuthService(
        repo=repo,  # type: ignore[arg-type]
        user_service=SimpleNamespace(),  # type: ignore[arg-type]
        mail_service=SimpleNamespace(),  # type: ignore[arg-type]
        verification_topic=SimpleNamespace(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_current_user_profile_returns_cached_profile_without_database_query() -> None:
    user_id = uuid4()
    cached_profile = UserInfo(
        id=user_id,
        name="Alex",
        email="alex@example.com",
        status=UserStatus.ACTIVE,
    )
    user_repo = UserRepositoryStub(cached_profile=cached_profile, user=None)
    registry = RegistryStub(user_repo)

    profile = await _service(registry).get_current_user(user_id, _ctx())

    assert profile == cached_profile
    assert registry.transaction_calls == 0
    assert user_repo.queried_user_id is None


@pytest.mark.asyncio
async def test_current_user_profile_loads_database_and_populates_cache_on_miss() -> None:
    user_id = uuid4()
    user = User(
        id=user_id,
        name="Alex",
        email="alex@example.com",
        status=UserStatus.ACTIVE,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    user_repo = UserRepositoryStub(cached_profile=None, user=user)
    registry = RegistryStub(user_repo)

    profile = await _service(registry).get_current_user(user_id, _ctx())

    assert registry.transaction_calls == 1
    assert user_repo.queried_user_id == user_id
    assert user_repo.cached_after_miss == profile
    assert profile.id == user_id
    assert profile.email == "alex@example.com"


@pytest.mark.asyncio
async def test_current_user_profile_rejects_a_missing_database_user() -> None:
    user_id = uuid4()
    user_repo = UserRepositoryStub(cached_profile=None, user=None)

    with pytest.raises(UnauthorizedException, match="User not found"):
        await _service(RegistryStub(user_repo)).get_current_user(user_id, _ctx())
