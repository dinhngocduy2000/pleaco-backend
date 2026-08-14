from datetime import datetime, timedelta, timezone
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest
import jwt

from app.common.context import AppContext
from app.common.enum.user_status import UserStatus
from app.common.exceptions import UnauthorizedException
from app.common.schemas.common import HashMapResponse
from app.common.schemas.user import UserInfo, UserJoinOption
from app.models.group import Group
from app.models.user import User
from app.repository.user import UserRepository
from app.services.auth import AuthService
import app.services.auth as auth_service_module


def _ctx() -> AppContext:
    return AppContext(trace_id=uuid4(), action="TEST_CURRENT_USER_PROFILE")


class UserRepositoryStub:
    def __init__(self, cached_profile: UserInfo | None, user: User | None) -> None:
        self.cached_profile = cached_profile
        self.user = user
        self.queried_user_id = None
        self.queried_options: UserJoinOption | None = None
        self.cached_after_miss: UserInfo | None = None
        self.cache_queries = 0
        self.joined_profile: UserInfo | None = None
        self.cached_token_queries: list[str] = []

    async def get_user_profile_with_cache(
        self, *, user_id, options: UserJoinOption | None = None, **kwargs
    ) -> UserInfo | None:
        if options is not None and options.included_owned_groups is True:
            self.queried_user_id = user_id
            self.queried_options = options
            return self.joined_profile
        self.cache_queries += 1
        if self.cached_profile is not None:
            return self.cached_profile
        self.queried_user_id = user_id
        self.queried_options = options
        self.cached_after_miss = (
            AuthService._to_user_info(self.user) if self.user is not None else None
        )
        return self.cached_after_miss

    async def get_token(self, hashed_token: str, ctx) -> str:
        self.cached_token_queries.append(hashed_token)
        return hashed_token

    async def set_hashed_token(self, hashed_token: str, ctx, *, expire: int) -> None:
        pass


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


@pytest.mark.asyncio
async def test_current_user_profile_joins_group_only_when_requested() -> None:
    user_id = uuid4()
    group_id = uuid4()
    user = User(
        id=user_id,
        name="Alex",
        email="alex@example.com",
        status=UserStatus.ACTIVE,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        active_group_id=group_id,
    )
    user_repo = UserRepositoryStub(
        cached_profile=UserInfo(
            id=user_id,
            name="Stale cache",
            email="alex@example.com",
            status=UserStatus.ACTIVE,
        ),
        user=user,
    )
    user_repo.joined_profile = UserInfo(
        id=user_id,
        name="Alex",
        email="alex@example.com",
        status=UserStatus.ACTIVE,
        group_id=group_id,
        group=HashMapResponse(value=group_id, label="Operations"),
    )

    profile = await _service(RegistryStub(user_repo)).get_current_user(
        user_id,
        _ctx(),
        UserJoinOption(included_owned_groups=True),
    )

    assert user_repo.cache_queries == 0
    assert user_repo.queried_options == UserJoinOption(included_owned_groups=True)
    assert user_repo.cached_after_miss is None
    assert profile.group_id == group_id
    assert profile.group == HashMapResponse(value=group_id, label="Operations")


@pytest.mark.asyncio
async def test_user_profile_repository_left_joins_active_group_when_requested() -> None:
    user_id = uuid4()
    group_id = uuid4()
    user = User(
        id=user_id,
        name="Alex",
        email="alex@example.com",
        status=UserStatus.ACTIVE,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        active_group_id=group_id,
    )
    group = Group(id=group_id, name="Operations", owner_id=user_id)

    class SessionStub:
        statement = None

        async def execute(self, statement):
            self.statement = statement
            return SimpleNamespace(first=lambda: (user, group))

    session = SessionStub()
    repository = UserRepository(redis_client=SimpleNamespace())

    profile = await repository.get_user_profile(
        session=session,
        user_id=user_id,
        ctx=_ctx(),
        options=UserJoinOption(included_owned_groups=True),
    )

    assert "LEFT OUTER JOIN groups" in str(session.statement)
    assert profile.group == HashMapResponse(value=group_id, label="Operations")


@pytest.mark.asyncio
async def test_refresh_token_reuses_cache_aside_user_profile_lookup(monkeypatch) -> None:
    user_id = uuid4()
    user_profile = UserInfo(
        id=user_id,
        name="Alex",
        email="alex@example.com",
        status=UserStatus.ACTIVE,
    )
    user_repo = UserRepositoryStub(cached_profile=user_profile, user=None)
    service = _service(RegistryStub(user_repo))
    monkeypatch.setattr(auth_service_module.settings, "SECRET_KEY", "test-secret")
    monkeypatch.setattr(auth_service_module.settings, "ALGORITHM", "HS256")
    monkeypatch.setattr(auth_service_module.settings, "ACCESS_TOKEN_EXPIRE_SECONDS", 120)
    monkeypatch.setattr(auth_service_module.settings, "REFRESH_TOKEN_EXPIRE_SECONDS", 600)
    refresh_token = jwt.encode(
        {
            "id": str(user_id),
            "token_type": "refresh",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        "test-secret",
        algorithm="HS256",
    )

    response = await service.refresh_token(refresh_token, _ctx())

    assert response.id == user_id
    assert user_repo.queried_user_id is None
    assert user_repo.cached_token_queries == [
        sha256(refresh_token.encode()).hexdigest()
    ]
