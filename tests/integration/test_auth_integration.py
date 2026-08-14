from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.common.enum.user_status import UserStatus
from app.common.schemas.user import UserLoginResponse
from app.handler.auth import AuthHandler
from app.router.auth import AuthRouter


async def _login_endpoint() -> None:
    return None


def test_login_route_is_registered_with_its_response_model() -> None:
    handler = SimpleNamespace(
        authenticate_user=_login_endpoint,
        register_user=_login_endpoint,
        validate_otp=_login_endpoint,
        logout=_login_endpoint,
        get_sso_auth_url=_login_endpoint,
        google_callback=_login_endpoint,
    )
    router = AuthRouter(handler=handler)  # type: ignore[arg-type]
    login_route = next(route for route in router.router.routes if route.path == "/login")

    assert login_route.methods == {"POST"}
    assert login_route.response_model is UserLoginResponse


@pytest.mark.asyncio
async def test_login_endpoint_returns_tokens_and_session_cookies_by_default() -> None:
    login_response = UserLoginResponse(
        id=uuid4(),
        name="Alex",
        email="alex@example.com",
        status=UserStatus.ACTIVE,
        access_token="access-token",
        refresh_token="refresh-token",
        expires_in=120,
    )

    class AuthServiceStub:
        async def login_user(self, login_request, ctx):
            assert login_request.is_save_session is False
            return login_response

    app = FastAPI()
    app.include_router(AuthRouter(AuthHandler(AuthServiceStub())).router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.post(
            "/login",
            json={"email": "alex@example.com", "password": "Valid!12"},
        )

    assert response.status_code == 200
    assert response.json()["access_token"] == "access-token"
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert all("Max-Age" not in cookie for cookie in cookies)
