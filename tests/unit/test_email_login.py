from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import bcrypt
import jwt
import pytest
from fastapi import HTTPException
from fastapi.responses import Response
from pydantic import ValidationError

import app.handler.auth as auth_handler_module
import app.services.auth as auth_service_module
from app.common.context import AppContext
from app.common.enum.user_status import UserStatus
from app.common.exceptions import BadRequestException
from app.common.middleware.auth_middleware import AuthMiddleware
from app.common.schemas.user import UserLogin, UserLoginResponse, UserQuery
from app.handler.auth import AuthHandler
from app.models.user import User
from app.services.auth import AuthService


def _ctx() -> AppContext:
    return AppContext(trace_id=uuid4(), action="TEST_LOGIN")


class UserRepositoryStub:
    def __init__(self, user: User | None) -> None:
        self.user = user
        self.queried_email: str | None = None
        self.cached_tokens: list[str] = []
        self.cache_expirations: list[int] = []

    async def get(self, *, query: UserQuery, **kwargs) -> User | None:
        self.queried_email = query.email
        return self.user

    async def set_hashed_token(
        self, hashed_token: str, *, expire: int, **kwargs
    ) -> None:
        self.cached_tokens.append(hashed_token)
        self.cache_expirations.append(expire)


class RegistryStub:
    def __init__(self, user_repo: UserRepositoryStub) -> None:
        self._user_repo = user_repo

    def user_repo(self) -> UserRepositoryStub:
        return self._user_repo

    async def transaction_wrapper(self, callback):
        return await callback(SimpleNamespace())


def _service(user_repo: UserRepositoryStub) -> AuthService:
    return AuthService(
        repo=RegistryStub(user_repo),  # type: ignore[arg-type]
        user_service=SimpleNamespace(),  # type: ignore[arg-type]
        mail_service=SimpleNamespace(),  # type: ignore[arg-type]
        verification_topic=SimpleNamespace(),  # type: ignore[arg-type]
    )


def _user(*, status: UserStatus = UserStatus.ACTIVE, password: str = "Valid!12") -> User:
    return User(
        id=uuid4(),
        name="Alex",
        email="alex@example.com",
        password=bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
        status=status,
    )


def _configure_token_settings(monkeypatch) -> None:
    monkeypatch.setattr(auth_service_module.settings, "SECRET_KEY", "test-secret")
    monkeypatch.setattr(auth_service_module.settings, "ALGORITHM", "HS256")
    monkeypatch.setattr(auth_service_module.settings, "ACCESS_TOKEN_EXPIRE_SECONDS", 120)
    monkeypatch.setattr(auth_service_module.settings, "REFRESH_TOKEN_EXPIRE_SECONDS", 600)
    monkeypatch.setattr(auth_handler_module.settings, "ACCESS_TOKEN_EXPIRE_SECONDS", 120)
    monkeypatch.setattr(auth_handler_module.settings, "REFRESH_TOKEN_EXPIRE_SECONDS", 600)


def test_login_schema_requires_valid_email_and_non_empty_password() -> None:
    with pytest.raises(ValidationError):
        UserLogin(email="not-an-email", password="password")
    with pytest.raises(ValidationError):
        UserLogin(email="alex@example.com", password="")

    login = UserLogin(email="Alex@Example.com", password="password")
    assert str(login.email) == "Alex@example.com"
    assert login.is_save_session is False


@pytest.mark.asyncio
async def test_login_returns_tokens_with_credential_claims_and_caches_access_token(
    monkeypatch,
) -> None:
    _configure_token_settings(monkeypatch)
    user_repo = UserRepositoryStub(_user())

    response = await _service(user_repo).login_user(
        UserLogin(email="ALEX@example.com", password="Valid!12"), _ctx()
    )

    access_payload = jwt.decode(
        response.access_token, "test-secret", algorithms=["HS256"]
    )
    refresh_payload = jwt.decode(
        response.refresh_token, "test-secret", algorithms=["HS256"]
    )
    assert user_repo.queried_email == "alex@example.com"
    assert response.expires_in == 120
    assert access_payload["token_type"] == "access"
    assert refresh_payload["token_type"] == "refresh"
    for payload in (access_payload, refresh_payload):
        assert payload["id"] == str(user_repo.user.id)
        assert payload["email"] == "alex@example.com"
        assert payload["status"] == UserStatus.ACTIVE.value
        assert payload["is_pending"] is None
        assert payload["active_group_id"] is None
        assert "exp_time" in payload
    assert refresh_payload["exp"] - access_payload["exp"] >= 470
    assert sha256(response.access_token.encode()).hexdigest() in user_repo.cached_tokens
    assert sha256(response.refresh_token.encode()).hexdigest() in user_repo.cached_tokens
    assert user_repo.cache_expirations == [120, 600]


@pytest.mark.asyncio
async def test_login_rejects_missing_inactive_and_invalid_credentials() -> None:
    missing_repo = UserRepositoryStub(None)
    with pytest.raises(BadRequestException, match="Account does not exist"):
        await _service(missing_repo).login_user(
            UserLogin(email="alex@example.com", password="Valid!12"), _ctx()
        )

    inactive_repo = UserRepositoryStub(_user(status=UserStatus.INACTIVE))
    with pytest.raises(BadRequestException, match="Account is not active"):
        await _service(inactive_repo).login_user(
            UserLogin(email="alex@example.com", password="Valid!12"), _ctx()
        )

    invalid_password_repo = UserRepositoryStub(_user())
    with pytest.raises(BadRequestException, match="Incorrect password"):
        await _service(invalid_password_repo).login_user(
            UserLogin(email="alex@example.com", password="Wrong!12"), _ctx()
        )


def test_login_cookies_default_to_session_and_can_be_persisted(monkeypatch) -> None:
    _configure_token_settings(monkeypatch)
    login_response = UserLoginResponse(
        id=uuid4(),
        name="Alex",
        email="alex@example.com",
        status=UserStatus.ACTIVE,
        access_token="access-token",
        refresh_token="refresh-token",
        expires_in=120,
    )
    handler = AuthHandler(SimpleNamespace())

    session_response = Response()
    handler._set_cookies_tokens(session_response, login_response)
    session_cookies = session_response.headers.getlist("set-cookie")
    assert len(session_cookies) == 2
    assert all("Max-Age" not in cookie for cookie in session_cookies)
    assert all("HttpOnly" in cookie and "Secure" in cookie for cookie in session_cookies)

    persistent_response = Response()
    handler._set_cookies_tokens(persistent_response, login_response, is_save_session=True)
    persistent_cookies = persistent_response.headers.getlist("set-cookie")
    assert "Max-Age=120" in persistent_cookies[0]
    assert "Max-Age=600" in persistent_cookies[1]


def test_access_middleware_rejects_refresh_token(monkeypatch) -> None:
    _configure_token_settings(monkeypatch)
    refresh_token = jwt.encode(
        {
            "id": str(uuid4()),
            "email": "alex@example.com",
            "status": UserStatus.ACTIVE.value,
            "is_pending": None,
            "active_group_id": None,
            "token_type": "refresh",
            "exp": 4_102_444_800,
        },
        "test-secret",
        algorithm="HS256",
    )
    monkeypatch.setattr(AuthMiddleware, "auth_service", SimpleNamespace())

    with pytest.raises(HTTPException, match="Unauthorized"):
        AuthMiddleware._validate_decoded_token(refresh_token, _ctx())
