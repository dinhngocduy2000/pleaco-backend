import asyncio
import secrets
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Tuple
from uuid import UUID

import httpx
from fastapi import Request, Response
from app.common.context import AppContext
from app.common.enum.user_status import UserStatus
from app.common.middleware.logger import Logger
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
from app.external.mail.mail import MailService
from app.external.queues.topics.user_verification import UserVerificationTopic
from app.models.user import User
from app.repository.registry import Registry
from sqlalchemy.ext.asyncio import AsyncSession
import bcrypt
from app.core.config import settings
import jwt
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests

from app.services.user import UserService

logger = Logger()


class AuthService:
    repo: Registry
    user_service: UserService
    mail_service: MailService
    verification_topic: UserVerificationTopic

    def __init__(
        self,
        repo: Registry,
        user_service: UserService,
        mail_service: MailService,
        verification_topic: UserVerificationTopic,
    ) -> None:
        self.repo = repo
        self.user_service = user_service
        self.mail_service = mail_service
        self.verification_topic = verification_topic

    def _validate_login_user(
        self, user: User, ctx: AppContext, login_request: UserLogin
    ) -> None:
        if user.status == UserStatus.INACTIVE or user.status == UserStatus.DELETED:
            logger.error(
                msg=f"Account is not active. Please verify your email. User status: {user.status}",
                context=ctx,
            )
            raise BadRequestException(
                "Account is not active. Please verify your email.")

        if not user.password:
            logger.error(
                msg="Account does not have a password set. Please use SSO login.",
                context=ctx,
            )
            raise BadRequestException("Incorrect password")

        try:
            password_is_valid = bcrypt.checkpw(
                login_request.password.encode(
                    "utf-8"), user.password.encode("utf-8")
            )
        except ValueError as e:
            logger.error(
                msg=f"Error while checking password.  Error: {e}",
                context=ctx,
            )
            password_is_valid = False

        if not password_is_valid:
            logger.error(
                msg="Incorrect password. Please check your credentials.",
                context=ctx,
            )
            raise BadRequestException("Incorrect password")

    def _credential_payload(self, user: User, expires_in: int) -> dict:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        credential = Credential(
            id=user.id,
            email=user.email,
            status=user.status,
            exp_time=expires_at,
            active_group_id=user.active_group_id,
        )
        payload = credential.model_dump(mode="json")
        payload["exp"] = expires_at
        return payload

    def _generate_access_token(self, user: User) -> str:
        payload = self._credential_payload(
            user, settings.ACCESS_TOKEN_EXPIRE_SECONDS)
        payload["token_type"] = "access"
        return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

    def _generate_refresh_token(self, user: User) -> str:
        payload = self._credential_payload(
            user, settings.REFRESH_TOKEN_EXPIRE_SECONDS)
        payload["token_type"] = "refresh"
        return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

    async def _generate_tokens(self, user: User, ctx: AppContext) -> UserLoginResponse:
        access_token = self._generate_access_token(user)
        refresh_token = self._generate_refresh_token(user)
        hashed_access_token = hashlib.sha256(
            access_token.encode("utf-8")).hexdigest()
        hashed_refresh_token = hashlib.sha256(
            refresh_token.encode("utf-8")).hexdigest()
        await asyncio.gather(
            self.repo.user_repo().set_hashed_token(
                hashed_access_token, ctx, expire=settings.ACCESS_TOKEN_EXPIRE_SECONDS
            ),
            self.repo.user_repo().set_hashed_token(
                hashed_refresh_token, ctx, expire=settings.REFRESH_TOKEN_EXPIRE_SECONDS
            ),
        )
        return UserLoginResponse(
            id=user.id,
            name=user.name,
            email=user.email,
            status=user.status,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=settings.ACCESS_TOKEN_EXPIRE_SECONDS,
        )

    async def _validate_new_user(
        self, user_create: UserCreate, ctx: AppContext, session: AsyncSession
    ) -> User | None:
        existing_user = await self.repo.user_repo().get(
            session=session,
            query=UserQuery(email=str(user_create.email).lower()),
            ctx=ctx,
        )
        if existing_user is not None:
            raise BadRequestException(
                "An account with this email already exists")
        return existing_user

    async def _send_otp_mail(self, user: User, ctx: AppContext) -> None:
        otp = generate_otp()
        await self.repo.user_repo().set_otp_code(user.email, otp, ctx=ctx)
        try:
            await self.verification_topic.publish_verification_email(user.email, otp)
        except Exception:
            # Registration is deliberately accepted after persistence. A repeat request for
            # an inactive account regenerates and requeues the verification email.
            logger.exception(
                msg="Unable to queue verification email; registration can be retried",
                context=ctx,
            )

    async def create_user(self, user_create: UserCreate, ctx: AppContext) -> None:
        normalized_email = str(user_create.email).lower()

        async def persist_user(session: AsyncSession) -> User:
            user = await self._validate_new_user(
                user_create, ctx=ctx, session=session
            )
            password_hash = bcrypt.hashpw(
                user_create.password.encode("utf-8"), bcrypt.gensalt()
            ).decode("utf-8")
            if user is None:
                user = User(
                    name=user_create.name,
                    email=normalized_email,
                    password=password_hash,
                    status=UserStatus.INACTIVE,
                )
            else:
                user.name = user_create.name
                user.password = password_hash
                user.status = UserStatus.INACTIVE
            return await self.repo.user_repo().save_user(session, user)

        user = await self.repo.transaction_wrapper(persist_user)
        await self._send_otp_mail(user, ctx=ctx)

    async def login_user(
        self, login_request: UserLogin, ctx: AppContext
    ) -> UserLoginResponse:
        normalized_email = str(login_request.email).lower()

        async def authenticate(session: AsyncSession) -> User:
            user = await self.repo.user_repo().get(
                session=session,
                query=UserQuery(email=normalized_email),
                ctx=ctx,
            )
            if user is None:
                raise BadRequestException("Account does not exist")
            self._validate_login_user(
                user, ctx=ctx, login_request=login_request)
            if user.status == UserStatus.PENDING:
                await self.repo.user_repo().update_user(
                    session=session,
                    user_id=user.id,
                    user_update=UserUpdate(status=UserStatus.ACTIVE),
                    ctx=ctx,
                )
            return user

        user = await self.repo.transaction_wrapper(authenticate)
        return await self._generate_tokens(user, ctx=ctx)

    def get_sso_auth_url(
        self, strategy: BaseSSOStrategy, ctx: AppContext
    ) -> Tuple[str, str]:
        return strategy.get_auth_url(ctx)

    async def _login_response_from_sso_idinfo(
        self, idinfo: dict, ctx: AppContext
    ) -> UserLoginResponse:
        email = idinfo.get("email")
        if not isinstance(email, str) or not email:
            raise BadRequestException("Google account email is required")

        normalized_email = email.lower()
        profile_name = idinfo.get("name")
        name = profile_name.strip() if isinstance(profile_name, str) else ""
        name = (name or normalized_email.split("@", maxsplit=1)[0])[:50]
        picture = idinfo.get("picture")
        image_url = picture[:255] if isinstance(picture, str) else None

        async def provision_user(session: AsyncSession) -> User:
            user = await self.repo.user_repo().get(
                session=session,
                query=UserQuery(email=normalized_email),
                ctx=ctx,
            )
            if user is None:
                user = User(
                    name=name,
                    email=normalized_email,
                    password=None,
                    image_url=image_url,
                    status=UserStatus.PENDING,
                )
                return await self.repo.user_repo().save_user(session, user)

            if user.status in (UserStatus.INACTIVE, UserStatus.PENDING):
                await self.repo.user_repo().update_user(
                    session=session,
                    user_id=user.id,
                    user_update=UserUpdate(status=UserStatus.ACTIVE),
                    ctx=ctx,
                )
                user.status = UserStatus.ACTIVE
            return user

        user = await self.repo.transaction_wrapper(provision_user)
        return await self._generate_tokens(user, ctx=ctx)

    async def login_with_sso_callback(
        self,
        strategy: BaseSSOStrategy,
        request: Request,
        ctx: AppContext,
    ) -> UserLoginResponse:
        idinfo = await strategy.callback(request, ctx)
        return await self._login_response_from_sso_idinfo(idinfo, ctx)

    async def validate_otp(
        self, otp_request: ValidateOTPRequest, ctx: AppContext
    ) -> None:
        email = str(otp_request.email).lower()
        stored_otp = await self.repo.user_repo().get_otp_code(email, ctx=ctx)
        if stored_otp is None or not secrets.compare_digest(stored_otp, otp_request.otp):
            raise BadRequestException("Invalid or expired verification code")

        async def validate_and_update_user(session: AsyncSession) -> None:
            user = await self.repo.user_repo().get(
                session=session,
                query=UserQuery(email=email),
                ctx=ctx,
            )
            if user is None or user.status != UserStatus.INACTIVE:
                raise BadRequestException(
                    "Invalid or expired verification code")
            await self.repo.user_repo().update_user(
                session=session,
                user_id=user.id,
                user_update=UserUpdate(status=UserStatus.PENDING),
                ctx=ctx,
            )

        await self.repo.transaction_wrapper(validate_and_update_user)
        await self.repo.user_repo().delete_otp_code(email, ctx=ctx)

    async def refresh_token(
        self, refresh_token: str, ctx: AppContext
    ) -> UserLoginResponse:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def get_current_user(self, user_id: UUID, ctx: AppContext) -> UserInfo:

        raise NotImplementedError("Pleaco-specific implementation is pending.")

    async def logout(
        self, ctx: AppContext, response: Response, request: Request
    ) -> None:
        try:
            access_token = request.cookies.get("access_token")
            refresh_token = request.cookies.get("refresh_token")
            hashed_access_token = hashlib.sha256(
                access_token.encode("utf-8")
            ).hexdigest()
            hashed_refresh_token = hashlib.sha256(
                refresh_token.encode("utf-8")
            ).hexdigest()

            await self.repo.user_repo().delete_token(hashed_access_token, ctx)
            await self.repo.user_repo().delete_token(hashed_refresh_token, ctx)
            response.delete_cookie("access_token")
            response.delete_cookie("refresh_token")
            return
        except Exception as e:
            logger.error(msg=f"Logout service: Exception: {e}", context=ctx)
            raise e
