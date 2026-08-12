import asyncio
import hashlib
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from uuid import UUID

import httpx
from fastapi import Request, Response
from app.common.context import AppContext
from app.common.enum.user_status import UserStatus
from app.common.middleware.logger import Logger
from app.common.schemas.mail import SendMailRequest
from app.common.schemas.user import (
    Credential,
    UserCreate,
    UserInfo,
    UserLogin,
    UserLoginResponse,
    UserQuery,
    UserUpdate,
    ValidateOTPRequest,
)
from app.common.exceptions import BadRequestException, UnauthorizedException
from app.common.utils.generate_otp import generate_otp
from app.core.sso_providers.base_sso import BaseSSOStrategy
from app.external.mail.jinja_templates import render_mail_html
from app.external.mail.mail import MailService
from app.models.user import User
from app.repository.registry import Registry
from sqlalchemy.ext.asyncio import AsyncSession
import bcrypt
from app.core.config import settings
import jwt
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests

from app.services.user import UserService

salt = bcrypt.gensalt()

logger = Logger()


class AuthService:
    repo: Registry
    user_service: UserService
    mail_service: MailService
    sso_strategy: BaseSSOStrategy | None

    def __init__(
        self,
        repo: Registry,
        user_service: UserService,
        mail_service: MailService,
    ) -> None:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    def set_sso_strategy(self, strategy: BaseSSOStrategy):
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    def _validate_login_user(
        self, user: User, ctx: AppContext, login_request: UserLogin
    ) -> None:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    def _generate_access_token(self, user: User) -> str:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    def _generate_refresh_token(self, user: User) -> str:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def _generate_tokens(self, user: User, ctx: AppContext) -> UserLoginResponse:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def _validate_new_user(
        self, user_create: UserCreate, ctx: AppContext, session: AsyncSession
    ) -> None:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def _send_otp_mail(self, user: User, ctx: AppContext) -> None:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def create_user(self, user_create: UserCreate, ctx: AppContext) -> None:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def login_user(
        self, login_request: UserLogin, ctx: AppContext
    ) -> UserLoginResponse:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    def get_sso_auth_url(self, ctx: AppContext) -> Tuple[str, str]:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def _login_response_from_sso_idinfo(
        self, idinfo: dict, ctx: AppContext
    ) -> UserLoginResponse:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def login_with_sso_callback(
        self, request: Request, ctx: AppContext
    ) -> UserLoginResponse:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def validate_otp(
        self, otp_request: ValidateOTPRequest, ctx: AppContext
    ) -> None:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def refresh_token(
        self, refresh_token: str, ctx: AppContext
    ) -> UserLoginResponse:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def get_current_user(self, user_id: UUID, ctx: AppContext) -> UserInfo:

        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def logout(
        self, ctx: AppContext, response: Response, request: Request
    ) -> None:
        raise NotImplementedError("Pleaco-specific implementation is pending.")
