from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import AnyHttpUrl


class AppEnvTypes(Enum):
    PROD: str = "prod"
    DEV: str = "dev"
    TEST: str = "test"


class BaseAppSettings(BaseSettings):
    app_env: AppEnvTypes = AppEnvTypes.DEV

    class Config:
        env_file = ".env"
        extra = "ignore"


class Settings(BaseAppSettings):
    """Application settings"""

    # Application
    APP_NAME: str = "PLEACO"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = True
    ENVIRONMENT: str = "development"
    API_URL: str = "http://localhost:8000"
    FRONTEND_URL: str = "http://localhost:3000"
    # Origin for absolute URLs in outbound email (images, links). Must be reachable from
    # the public internet (e.g. https://api.yourdomain.com). If unset, API_URL is used —
    # localhost will not work in Gmail because Google's image proxy cannot fetch it.
    EMAIL_PUBLIC_BASE_URL: Optional[str] = None
    # Database
    DATABASE_URL: str
    POOL_SIZE: int = 40
    MAX_OVERFLOW: int = 5
    POOL_RECYCLE: int = 600
    # API
    API_V1_PREFIX: str = "/api/v1"
    SECRET_KEY: str
    ALGORITHM: str
    ACCESS_TOKEN_EXPIRE_SECONDS: int
    REFRESH_TOKEN_EXPIRE_SECONDS: int = 60 * 60 * 24 * 7  # 7 days

    # SMTP / Email
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = ""
    SMTP_USE_TLS: bool = True
    OTP_CODE_EXPIRE_SECONDS: int = 600  # 10 minutes
    INVITATION_EXPIRE_SECONDS: int = 60 * 60 * 24 * 7  # 7 days

    # Cache / Redis
    REDIS_HOST: str = "localhost"
    REDIS_PASSWORD: str
    REDIS_PORT: int = 6379
    cache_token_hash: Optional[str] = "token-hashed"
    CACHE_OTP_CODE: Optional[str] = "CACHE_OTP_CODE"

    # Message queue / RabbitMQ
    # A complete AMQP URL keeps deployment configuration in one portable setting.
    RABBITMQ_URL: str = "amqp://guest:guest@localhost:5672/"
    RABBITMQ_CONNECT_TIMEOUT: float = 10.0

    # Google OAuth (for Sign in with Google)
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    # Backend callback URL (must match Google Cloud Console). e.g. http://localhost:8080/api/v1/auth/google/callback
    GOOGLE_REDIRECT_URI: str = ""
    # Where to redirect the browser after successful Google login. e.g. http://localhost:3000
    GOOGLE_FRONTEND_REDIRECT_URI: str = "http://localhost:3000"

    # CORS
    BACKEND_CORS_ORIGINS: List[AnyHttpUrl] = []

    model_config = SettingsConfigDict(
        env_file=".env", case_sensitive=True, extra="allow"
    )

    @property
    def email_public_base_url(self) -> str:
        """Base URL for email HTML (img src, etc.); strip trailing slash."""
        base = self.EMAIL_PUBLIC_BASE_URL or self.API_URL
        return base.rstrip("/")

    @property
    def fastapi_kwargs(self) -> Dict[str, Any]:
        return {
            "title": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "openapi_url": f"{settings.API_V1_PREFIX}/openapi.json",
            # Served in app/cmd/main.py so JS/CSS can use local files or a non-jsDelivr CDN.
            "docs_url": None,
            "redoc_url": f"{settings.API_V1_PREFIX}/redoc",
        }


settings = Settings()
