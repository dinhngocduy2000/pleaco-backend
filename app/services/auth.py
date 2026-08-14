import asyncio
import secrets
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Tuple
from uuid import UUID
from fastapi import Request, Response
from app.common.context import AppContext
from app.common.enum.user_status import UserStatus
from app.common.middleware.logger import Logger
from app.common.schemas.user import (
    Credential,
    UserCreate,
    UserInfo,
    UserJoinOption,
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
from app.services.user import UserService

logger = Logger()


class AuthService:
    """Coordinate local authentication, SSO, token lifecycle, and user profiles."""

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
        """Initialize the service with its persistence and external integrations.

        Args:
            repo: Registry providing database and Redis repositories.
            user_service: Service for user-domain operations.
            mail_service: Service for outbound email operations.
            verification_topic: Queue publisher for verification emails.
        """
        self.repo = repo
        self.user_service = user_service
        self.mail_service = mail_service
        self.verification_topic = verification_topic

    def _validate_login_user(
        self, user: User, ctx: AppContext, login_request: UserLogin
    ) -> None:
        """Validate that a local-password user can sign in.

        Args:
            user: Persisted user being authenticated.
            ctx: Request context used for logging.
            login_request: Submitted credentials.

        Raises:
            BadRequestException: If the account is inactive, lacks a password, or
                the password does not match.
        """
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

    @staticmethod
    def _to_user_info(user: User) -> UserInfo:
        """Map a password-bearing ORM user to its safe profile representation.

        Args:
            user: Persisted user entity.

        Returns:
            The corresponding user profile without password data.
        """
        return UserInfo(
            id=user.id,
            name=user.name,
            email=user.email,
            status=user.status,
            created_at=user.created_at,
            updated_at=user.updated_at,
            image_url=user.image_url,
            group_id=user.active_group_id,
        )

    def _credential_payload(self, user: UserInfo, expires_in: int) -> dict:
        """Build the JWT claim payload for a user profile.

        Args:
            user: Safe user profile used as the source of JWT claims.
            expires_in: Token lifetime in seconds.

        Returns:
            JSON-compatible credential claims with an expiration timestamp.
        """
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        credential = Credential(
            id=user.id,
            email=user.email,
            status=user.status,
            exp_time=expires_at,
            active_group_id=user.group_id,
        )
        payload = credential.model_dump(mode="json")
        payload["exp"] = expires_at
        return payload

    def _generate_access_token(self, user: UserInfo) -> str:
        """Issue a signed access token for a user profile.

        Args:
            user: Safe user profile to encode in the token.

        Returns:
            Signed access JWT.
        """
        payload = self._credential_payload(
            user, settings.ACCESS_TOKEN_EXPIRE_SECONDS)
        payload["token_type"] = "access"
        return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

    def _generate_refresh_token(self, user: UserInfo) -> str:
        """Issue a signed refresh token for a user profile.

        Args:
            user: Safe user profile to encode in the token.

        Returns:
            Signed refresh JWT.
        """
        payload = self._credential_payload(
            user, settings.REFRESH_TOKEN_EXPIRE_SECONDS)
        payload["token_type"] = "refresh"
        return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

    async def _generate_tokens(
        self, user: UserInfo, ctx: AppContext
    ) -> UserLoginResponse:
        """Generate and cache the access and refresh tokens for a user.

        Args:
            user: Safe user profile for token claims and response data.
            ctx: Request context used for cache operations.

        Returns:
            Login response containing the user profile and both tokens.
        """
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
        """Ensure no active account already uses the requested email address.

        Args:
            user_create: Registration request to validate.
            ctx: Request context used for repository logging.
            session: Transaction-scoped database session.

        Returns:
            The matching user when one exists; otherwise ``None``.

        Raises:
            BadRequestException: If an account already exists for the email address.
        """
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
        """Create, cache, and enqueue an email verification OTP.

        Registration remains successful if queue publication fails, allowing the
        inactive user to retry and receive a new OTP.

        Args:
            user: Newly registered user receiving the OTP.
            ctx: Request context used for cache operations and logging.
        """
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
        """Create an inactive local account and start email verification.

        Args:
            user_create: Validated registration data.
            ctx: Request context used for the transaction and OTP workflow.

        Raises:
            BadRequestException: If the email address is already registered.
        """
        normalized_email = str(user_create.email).lower()

        async def persist_user(session: AsyncSession) -> User:
            """Persist the inactive user in the enclosing transaction."""
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
        """Authenticate local credentials and issue a token pair.

        Args:
            login_request: Submitted email, password, and session preference.
            ctx: Request context used for persistence and token caching.

        Returns:
            User profile and newly issued access and refresh tokens.

        Raises:
            BadRequestException: If the account is missing or credentials are invalid.
        """
        normalized_email = str(login_request.email).lower()

        async def authenticate(session: AsyncSession) -> User:
            """Load and validate the user within the authentication transaction."""
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
        return await self._generate_tokens(self._to_user_info(user), ctx=ctx)

    def get_sso_auth_url(
        self, strategy: BaseSSOStrategy, ctx: AppContext
    ) -> Tuple[str, str]:
        """Get an SSO authorization URL and its anti-forgery state value.

        Args:
            strategy: Configured SSO provider strategy.
            ctx: Request context forwarded to the strategy.

        Returns:
            Authorization URL and state token.
        """
        return strategy.get_auth_url(ctx)

    async def _login_response_from_sso_idinfo(
        self, idinfo: dict, ctx: AppContext
    ) -> UserLoginResponse:
        """Provision or activate an SSO user from verified identity claims.

        Args:
            idinfo: Verified identity-provider claims.
            ctx: Request context used for persistence and token caching.

        Returns:
            Login response with newly issued tokens.

        Raises:
            BadRequestException: If the provider response has no usable email.
        """
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
            """Create or reactivate the SSO account in the current transaction."""
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
        return await self._generate_tokens(self._to_user_info(user), ctx=ctx)

    async def login_with_sso_callback(
        self,
        strategy: BaseSSOStrategy,
        request: Request,
        ctx: AppContext,
    ) -> UserLoginResponse:
        """Complete an SSO callback and issue tokens for the resolved account.

        Args:
            strategy: SSO provider strategy handling the callback.
            request: Incoming provider callback request.
            ctx: Request context for provider and persistence operations.

        Returns:
            Login response with newly issued tokens.
        """
        idinfo = await strategy.callback(request, ctx)
        return await self._login_response_from_sso_idinfo(idinfo, ctx)

    async def validate_otp(
        self, otp_request: ValidateOTPRequest, ctx: AppContext
    ) -> None:
        """Validate a cached OTP and transition an inactive account to pending.

        Args:
            otp_request: Email address and OTP submitted for verification.
            ctx: Request context used for cache and database operations.

        Raises:
            BadRequestException: If the OTP is expired, invalid, or the account
                cannot be verified in its current state.
        """
        email = str(otp_request.email).lower()
        stored_otp = await self.repo.user_repo().get_otp_code(email, ctx=ctx)
        if stored_otp is None or not secrets.compare_digest(stored_otp, otp_request.otp):
            raise BadRequestException("Invalid or expired verification code")

        async def validate_and_update_user(session: AsyncSession) -> None:
            """Verify the account state and persist its pending status."""
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
        """Validate a refresh token and replace it with a new token pair.

        Args:
            refresh_token: Signed refresh JWT supplied by the client.
            ctx: Request context used for cache checks and logging.

        Returns:
            Login response containing the replacement tokens.

        Raises:
            BadRequestException: If the token is invalid, expired, revoked, has the
                wrong type, or its user no longer exists.
        """
        async def _refresh_token() -> UserLoginResponse:
            """Decode, validate, and rotate the supplied refresh token."""
            logger.info(msg=f"Decoding refresh token...", context=ctx)
            try:
                token = jwt.decode(
                    refresh_token, settings.SECRET_KEY, algorithms=[
                        settings.ALGORITHM]
                )
                hashed_refresh_token = hashlib.sha256(
                    refresh_token.encode("utf-8")
                ).hexdigest()
                cached_refresh_token = await self.repo.user_repo().get_token(
                    hashed_refresh_token, ctx
                )
                if cached_refresh_token is None:
                    logger.error(
                        msg=f"Refresh token not found in cache", context=ctx)
                    raise BadRequestException(message="Invalid refresh token")

                if token["token_type"] != "refresh":
                    logger.error(msg=f"Invalid token type", context=ctx)
                    raise BadRequestException(message="Invalid token type")

                user = await self._get_user_profile(UUID(token["id"]), ctx)
                if user is None:
                    logger.error(
                        msg=f"User with id {token['id']} not found", context=ctx
                    )
                    raise BadRequestException(message="User not found")
                if token["exp"] < datetime.now(timezone.utc).timestamp():
                    logger.error(msg=f"Token expired", context=ctx)
                    raise BadRequestException(message="Token expired")
                logger.info(
                    msg=f"Token decoded successfully, generating new tokens...",
                    context=ctx,
                )
                return await self._generate_tokens(user, ctx)
            except jwt.DecodeError as e:
                logger.error(
                    msg=f"Invalid refresh token: DecodeError", context=ctx)
                raise BadRequestException(message="Invalid refresh token")
            except jwt.ExpiredSignatureError as e:
                logger.error(
                    msg=f"Invalid refresh token: ExpiredSignatureError", context=ctx
                )
                raise BadRequestException(message="Token expired")
            except jwt.InvalidTokenError as e:
                logger.error(
                    msg=f"Invalid refresh token: InvalidTokenError", context=ctx
                )
                raise BadRequestException(message="Invalid refresh token")
            except jwt.InvalidSignatureError as e:
                logger.error(
                    msg=f"Invalid refresh token: InvalidSignatureError", context=ctx
                )
                raise BadRequestException(message="Invalid refresh token")
            except jwt.InvalidAlgorithmError as e:
                logger.error(
                    msg=f"Invalid refresh token: InvalidAlgorithmError", context=ctx
                )
                raise BadRequestException(message="Invalid refresh token")
            except jwt.InvalidKeyError as e:
                logger.error(
                    msg=f"Invalid refresh token: InvalidKeyError", context=ctx)
                raise BadRequestException(message="Invalid refresh token")
            except Exception as e:
                logger.error(
                    msg=f"Invalid refresh token: Exception: {e}", context=ctx)
                raise e

        return await _refresh_token()

    async def _get_user_profile(
        self,
        user_id: UUID,
        ctx: AppContext,
        options: UserJoinOption | None = None,
    ) -> UserInfo | None:
        """Load a profile from Redis first, then the database on a cache miss.

        Args:
            user_id: Identifier of the user to load.
            ctx: Request context used for logging and data access.
            options: Optional group-loading configuration forwarded to the repository.

        Returns:
            The cached or persisted user profile, or ``None`` when absent.
        """
        async def get_user_profile(session: AsyncSession) -> UserInfo | None:
            """Load the profile through the reusable repository cache-aside query."""
            return await self.repo.user_repo().get_user_profile_with_cache(
                session=session,
                user_id=user_id,
                ctx=ctx,
                options=options,
            )

        return await self.repo.transaction_wrapper(get_user_profile)

    async def get_current_user(
        self,
        user_id: UUID,
        ctx: AppContext,
    ) -> UserInfo:
        """Return the authenticated user's profile with its active group summary.

        Args:
            user_id: Identifier extracted from the authenticated credential.
            ctx: Request context used for data access and logging.

        Returns:
            Current user's profile and active-group information when present.

        Raises:
            UnauthorizedException: If no matching user exists.
        """
        user_profile = await self._get_user_profile(user_id, ctx, options=UserJoinOption(included_owned_groups=True))
        if user_profile is None:
            logger.error(msg=f"User with id {user_id} not found", context=ctx)
            raise UnauthorizedException("User not found")
        return user_profile

    async def logout(
        self, ctx: AppContext, response: Response, request: Request
    ) -> None:
        """Revoke cached session tokens and clear their response cookies.

        Args:
            ctx: Request context used for cache operations and logging.
            response: HTTP response whose authentication cookies are cleared.
            request: HTTP request containing the current authentication cookies.
        """
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
