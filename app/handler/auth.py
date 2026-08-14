from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4
from fastapi import Depends, Query, Request, Response
from fastapi.responses import RedirectResponse
from app.common.context import AppContext
from app.common.enum.context_actions import (
    AUTHENTICATE_USER,
    GET_CURRENT_USER_PROFILE,
    GOOGLE_AUTHENTICATE,
    LOGOUT,
    REFRESH_TOKEN,
    REGISTER_USER,
    TRACK_SESSION,
    VALIDATE_OTP,
)
from app.common.enum.sso_providers import SSO_PROVIDERS
from app.common.exceptions import BadRequestException, UnauthorizedException
from app.common.exceptions.decorator import exception_handler
from app.common.middleware.auth_middleware import AuthMiddleware
from app.common.middleware.logger import Logger
from app.common.schemas.common import BaseResponse
from app.common.schemas.user import (
    Credential,
    SSOAuthUrlResponse,
    SSOLoginResponse,
    RefreshTokenRequest,
    UserCreate,
    UserInfo,
    UserLogin,
    UserLoginResponse,
    ValidateOTPRequest,
)
from app.core.sso_providers.sso_factory import SSOFactory
from app.services.auth import AuthService
from app.core.config import settings

logger = Logger()


class AuthHandler:
    service: AuthService

    def __init__(self, service: AuthService) -> None:
        self.service = service

    def _set_cookies_tokens(
        self,
        response: Response,
        login_response: UserLoginResponse,
        is_save_session: bool = False,
    ) -> None:
        response.set_cookie(
            key="access_token",
            value=login_response.access_token,
            httponly=True,
            secure=True,  # critical on http
            samesite="lax",
            max_age=login_response.expires_in if is_save_session else None,
        )
        response.set_cookie(
            key="refresh_token",
            value=login_response.refresh_token,
            httponly=True,
            secure=True,  # critical on http
            samesite="lax",
            max_age=(
                settings.REFRESH_TOKEN_EXPIRE_SECONDS if is_save_session else None
            ),
        )

    @exception_handler
    async def authenticate_user(
        self, login_request: UserLogin, response: Response, request: Request
    ) -> UserLoginResponse:
        ctx = AppContext(trace_id=uuid4(), action=AUTHENTICATE_USER)
        logger.info(msg=f"Starting login endpoint: {request.url}", context=ctx)
        login_response = await self.service.login_user(login_request, ctx=ctx)
        self._set_cookies_tokens(
            response=response,
            login_response=login_response,
            is_save_session=login_request.is_save_session,
        )
        return login_response

    @exception_handler
    async def get_sso_auth_url(
        self,
        response: Response,
        request: Request,
        provider: SSO_PROVIDERS = Query(..., description="SSO Auth Provilder"),
    ) -> SSOLoginResponse:
        ctx = SSOFactory.resolve_sso_context(provider)
        strategy = SSOFactory.resolve_sso_strategy(provider)
        url, state = self.service.get_sso_auth_url(strategy, ctx=ctx)
        response.set_cookie(
            key=strategy.state_cookie_name,
            value=state,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=600,
        )
        return SSOLoginResponse(
            data=SSOAuthUrlResponse(url=url),
            message="SSO authorization URL generated",
            statusCode=200,
        )

    @exception_handler
    async def google_callback(
        self,
        request: Request,
    ) -> RedirectResponse:
        provider = SSO_PROVIDERS.GOOGLE
        ctx = SSOFactory.resolve_sso_context(provider)
        strategy = SSOFactory.resolve_sso_strategy(provider)
        login_response = await self.service.login_with_sso_callback(
            strategy=strategy,
            request=request,
            ctx=ctx,
        )
        response = RedirectResponse(
            url=settings.GOOGLE_FRONTEND_REDIRECT_URI,
            status_code=302,
        )
        self._set_cookies_tokens(
            response=response,
            login_response=login_response,
            is_save_session=True,
        )
        response.delete_cookie(strategy.state_cookie_name)
        return response

    @exception_handler
    async def register_user(
        self, request: Request, user_create: UserCreate
    ) -> BaseResponse[str]:
        ctx = AppContext(trace_id=uuid4(), action=REGISTER_USER)
        logger.info(msg=f"Starting registration endpoint: {request.url}", context=ctx)
        await self.service.create_user(user_create, ctx=ctx)
        return BaseResponse(
            data="Verification email queued",
            message="Registration successful. Please verify your email.",
            statusCode=201,
        )

    @exception_handler
    async def refresh_token(
        self,
        request: Request,
        response: Response,
        refresh_token_request: RefreshTokenRequest,
    ) -> str:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    @exception_handler
    async def get_current_user_profile(
        self,
        request: Request,
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> BaseResponse[UserInfo]:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    @exception_handler
    async def track_session(
        self, credential: Credential = Depends(AuthMiddleware.auth_middleware)
    ) -> str:

        raise NotImplementedError("Pleaco-specific implementation is pending.")

    @exception_handler
    async def logout(self, response: Response, request: Request) -> str:
        ctx = AppContext(trace_id=uuid4(), action=LOGOUT)
        logger.info(msg=f"Starting logout endpoint: {request.url}", context=ctx)
        await self.service.logout(ctx=ctx, response=response, request=request)
        return "Success"

    @exception_handler
    async def validate_otp(
        self, request: Request, validate_otp_request: ValidateOTPRequest
    ) -> BaseResponse[str]:
        ctx = AppContext(trace_id=uuid4(), action=VALIDATE_OTP)
        logger.info(msg=f"Starting OTP validation endpoint: {request.url}", context=ctx)
        await self.service.validate_otp(validate_otp_request, ctx=ctx)
        return BaseResponse(
            data="Account verified",
            message="Your account has been verified.",
            statusCode=200,
        )
