from datetime import datetime, timezone
import hashlib
from typing import Optional
import uuid
from fastapi import HTTPException, Request, status
import jwt
from app.common.context import AppContext
from app.common.enum.context_actions import AUTHENTICATE_USER
from app.common.enum.user_status import UserStatus
from app.common.middleware.logger import Logger
from app.common.schemas.user import Credential
from app.core.config import settings
from app.services.auth import AuthService

logger = Logger()


class AuthMiddleware:
    auth_service: AuthService

    @classmethod
    def init(cls, auth_service: AuthService) -> None:
        cls.auth_service = auth_service

    @classmethod
    async def _validate_cached_token(
        cls, access_token: Optional[str], ctx: AppContext
    ) -> None:
        if access_token is None:
            logger.error(msg=f"Access token not found in cookies...", context=ctx)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized"
            )

        hashed_access_token = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
        cached_access_token = await cls.auth_service.repo.user_repo().get_token(
            hashed_access_token, ctx
        )
        if cached_access_token is None:
            logger.error(msg=f"Access token not found in cache...", context=ctx)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized"
            )

    @classmethod
    def _validate_decoded_token(cls, access_token: str, ctx: AppContext) -> dict:
        decoded_token = jwt.decode(
            access_token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        logger.info(msg=f"User status: {decoded_token['status']}", context=ctx)
        if decoded_token["status"] in [
            UserStatus.DELETED,
            UserStatus.INACTIVE,
        ]:
            logger.error(msg=f"User is not active or deleted", context=ctx)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User is not active or deleted",
            )
        return decoded_token

    @classmethod
    def _generate_credential(cls, decoded_token: dict) -> Credential:
        exp_time = decoded_token["exp"]
        user_id = uuid.UUID(decoded_token["id"])
        user_email = decoded_token["email"]
        is_pending = decoded_token["is_pending"]
        active_group_id = decoded_token["active_group_id"]
        user_status = decoded_token["status"]

        credential: Credential = Credential(
            id=user_id,
            email=user_email,
            status=user_status,
            is_pending=is_pending,
            exp_time=exp_time,
        )
        if active_group_id is not None:
            credential.active_group_id = active_group_id

        return credential

    @classmethod
    async def _validate_cookie_tokens(
        cls, request: Request, ctx: AppContext
    ) -> Credential:
        logger.info(msg=f"Validating tokens in cookies and cache...", context=ctx)
        access_token = request.cookies.get("access_token")
        await cls._validate_cached_token(access_token, ctx)

        logger.info(msg=f"Token found, decoding token...", context=ctx)

        decoded_token = cls._validate_decoded_token(access_token=access_token, ctx=ctx)

        logger.info(msg=f"Token decoded successfully, checking expiration", context=ctx)
        if datetime.now(timezone.utc) > datetime.fromtimestamp(
            decoded_token["exp"], timezone.utc
        ):
            logger.error(msg=f"Token expired", context=ctx)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized"
            )
        logger.info(msg=f"Token not expired, generating credential...", context=ctx)
        credential: Credential = cls._generate_credential(decoded_token=decoded_token)
        return credential

    @classmethod
    async def auth_middleware(cls, request: Request) -> Credential:
        try:
            ctx = AppContext(trace_id=uuid.uuid4(), action=AUTHENTICATE_USER)
            credential: Credential = await cls._validate_cookie_tokens(request, ctx)
            logger.info(msg=f"Credential authorized", context=ctx)

            return credential

        except Exception as e:
            logger.error(f"Exception while handling auth middleware: {e}")
            raise e
