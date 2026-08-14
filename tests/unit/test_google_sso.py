from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from fastapi import Request
from fastapi.responses import Response

import app.core.sso_providers.google_sso as google_sso_module
import app.handler.auth as auth_handler_module
import app.services.auth as auth_service_module
from app.common.context import AppContext
from app.common.enum.sso_providers import SSO_PROVIDERS
from app.common.enum.user_status import UserStatus
from app.common.exceptions import BadRequestException
from app.common.schemas.user import UserLoginResponse, UserQuery, UserUpdate
from app.core.sso_providers.google_sso import GoogleSSOStrategy
from app.handler.auth import AuthHandler
from app.models.user import User
from app.services.auth import AuthService


def _ctx() -> AppContext:
    return AppContext(trace_id=uuid4(), action="GOOGLE_AUTHENTICATE")


def _request(*, query: str = "", cookie: str = "") -> Request:
    headers = [(b"cookie", cookie.encode())] if cookie else []
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/sso/callback",
            "query_string": query.encode(),
            "headers": headers,
        }
    )


def _configure_google(monkeypatch) -> None:
    monkeypatch.setattr(google_sso_module.settings, "GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setattr(
        google_sso_module.settings, "GOOGLE_CLIENT_SECRET", "client-secret"
    )
    monkeypatch.setattr(
        google_sso_module.settings,
        "GOOGLE_REDIRECT_URI",
        "https://api.example.com/api/v1/auth/sso/callback",
    )


def _configure_tokens(monkeypatch) -> None:
    monkeypatch.setattr(auth_service_module.settings, "SECRET_KEY", "test-secret")
    monkeypatch.setattr(auth_service_module.settings, "ALGORITHM", "HS256")
    monkeypatch.setattr(auth_service_module.settings, "ACCESS_TOKEN_EXPIRE_SECONDS", 120)
    monkeypatch.setattr(auth_service_module.settings, "REFRESH_TOKEN_EXPIRE_SECONDS", 600)
    monkeypatch.setattr(auth_handler_module.settings, "REFRESH_TOKEN_EXPIRE_SECONDS", 600)
    monkeypatch.setattr(
        auth_handler_module.settings,
        "GOOGLE_FRONTEND_REDIRECT_URI",
        "https://app.example.com",
    )


class UserRepositoryStub:
    def __init__(self, user: User | None = None) -> None:
        self.user = user
        self.saved_user: User | None = None
        self.updated_user: UserUpdate | None = None
        self.cache_expirations: list[int] = []

    async def get(self, *, query: UserQuery, **kwargs) -> User | None:
        assert query.email == "alex@example.com"
        return self.user

    async def save_user(self, session, user: User) -> User:
        if user.id is None:
            user.id = uuid4()
        self.user = user
        self.saved_user = user
        return user

    async def update_user(self, *, user_update: UserUpdate, **kwargs) -> None:
        self.updated_user = user_update

    async def set_hashed_token(self, hashed_token: str, ctx, *, expire: int) -> None:
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


def test_google_strategy_generates_authorization_url_and_state(monkeypatch) -> None:
    _configure_google(monkeypatch)

    url, state = GoogleSSOStrategy().get_auth_url(_ctx())

    params = parse_qs(urlparse(url).query)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert params["client_id"] == ["client-id"]
    assert params["redirect_uri"] == ["https://api.example.com/api/v1/auth/sso/callback"]
    assert params["state"] == [state]
    assert len(state) >= 32


@pytest.mark.asyncio
async def test_google_callback_validates_state_code_and_verified_email(monkeypatch) -> None:
    _configure_google(monkeypatch)

    class ResponseStub:
        status_code = 200

        def json(self):
            return {"id_token": "google-id-token"}

    class ClientStub:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            assert kwargs["data"]["code"] == "authorization-code"
            return ResponseStub()

    monkeypatch.setattr(google_sso_module.httpx, "AsyncClient", ClientStub)
    monkeypatch.setattr(
        google_sso_module.google_id_token,
        "verify_oauth2_token",
        lambda *args: {"email": "alex@example.com", "email_verified": True},
    )
    strategy = GoogleSSOStrategy()

    claims = await strategy.callback(
        _request(
            query="state=expected-state&code=authorization-code",
            cookie="google_oauth_state=expected-state",
        ),
        _ctx(),
    )
    assert claims["email"] == "alex@example.com"

    with pytest.raises(BadRequestException, match="Invalid state"):
        await strategy.callback(_request(query="state=wrong&code=code"), _ctx())
    with pytest.raises(BadRequestException, match="Google sign-in failed"):
        await strategy.callback(
            _request(query="state=expected-state", cookie="google_oauth_state=expected-state"),
            _ctx(),
        )


@pytest.mark.asyncio
async def test_google_callback_rejects_unverified_email(monkeypatch) -> None:
    _configure_google(monkeypatch)

    class ResponseStub:
        status_code = 200

        def json(self):
            return {"id_token": "google-id-token"}

    class ClientStub:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return ResponseStub()

    monkeypatch.setattr(google_sso_module.httpx, "AsyncClient", ClientStub)
    monkeypatch.setattr(
        google_sso_module.google_id_token,
        "verify_oauth2_token",
        lambda *args: {"email": "alex@example.com", "email_verified": False},
    )

    with pytest.raises(BadRequestException, match="not verified"):
        await GoogleSSOStrategy().callback(
            _request(query="state=state&code=code", cookie="google_oauth_state=state"),
            _ctx(),
        )


@pytest.mark.asyncio
async def test_sso_provisions_passwordless_pending_user_and_transitions_existing_user(
    monkeypatch,
) -> None:
    _configure_tokens(monkeypatch)
    claims = {
        "email": "ALEX@example.com",
        "email_verified": True,
        "name": "Alex Google",
        "picture": "https://example.com/alex.png",
    }
    new_repo = UserRepositoryStub()

    new_response = await _service(new_repo)._login_response_from_sso_idinfo(claims, _ctx())

    assert new_repo.saved_user is not None
    assert new_repo.saved_user.password is None
    assert new_repo.saved_user.status == UserStatus.PENDING
    assert new_repo.saved_user.image_url == "https://example.com/alex.png"
    assert new_response.status == UserStatus.PENDING
    assert new_repo.cache_expirations == [120, 600]

    existing = User(
        id=uuid4(),
        name="Alex",
        email="alex@example.com",
        password="password-hash",
        status=UserStatus.INACTIVE,
    )
    existing_repo = UserRepositoryStub(existing)
    response = await _service(existing_repo)._login_response_from_sso_idinfo(claims, _ctx())

    assert existing_repo.saved_user is None
    assert existing_repo.updated_user is not None
    assert existing.status == UserStatus.PENDING
    assert response.status == UserStatus.PENDING


@pytest.mark.asyncio
async def test_sso_handler_sets_state_and_redirects_with_persistent_tokens(monkeypatch) -> None:
    _configure_tokens(monkeypatch)
    login_response = UserLoginResponse(
        id=uuid4(),
        name="Alex",
        email="alex@example.com",
        status=UserStatus.PENDING,
        access_token="access-token",
        refresh_token="refresh-token",
        expires_in=120,
    )

    class StrategyStub:
        state_cookie_name = "google_oauth_state"

    strategy = StrategyStub()
    monkeypatch.setattr(
        auth_handler_module.SSOFactory,
        "resolve_sso_strategy",
        lambda provider: strategy,
    )
    monkeypatch.setattr(
        auth_handler_module.SSOFactory,
        "resolve_sso_context",
        lambda provider: _ctx(),
    )

    class ServiceStub:
        def get_sso_auth_url(self, received_strategy, ctx):
            assert received_strategy is strategy
            return "https://accounts.google.com/login", "oauth-state"

        async def login_with_sso_callback(self, **kwargs):
            assert kwargs["strategy"] is strategy
            return login_response

    handler = AuthHandler(ServiceStub())
    state_response = Response()
    auth_url_response = await handler.get_sso_auth_url(
        response=state_response,
        request=_request(),
        provider=SSO_PROVIDERS.GOOGLE,
    )
    callback_response = await handler.google_callback(_request())

    assert auth_url_response.data.url == "https://accounts.google.com/login"
    assert "Max-Age=600" in state_response.headers["set-cookie"]
    assert callback_response.headers["location"] == "https://app.example.com"
    cookies = callback_response.headers.getlist("set-cookie")
    assert "Max-Age=120" in cookies[0]
    assert "Max-Age=600" in cookies[1]
    assert any("google_oauth_state=\"\"" in cookie for cookie in cookies)
