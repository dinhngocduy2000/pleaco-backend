from fastapi import APIRouter, status
from fastapi.responses import RedirectResponse

from app.common.schemas.common import BaseResponse
from app.common.schemas.user import (
    SSOLoginResponse,
    UserInfo,
)
from app.handler.auth import AuthHandler


class AuthRouter:
    router: APIRouter
    handler: AuthHandler

    def __init__(self, handler: AuthHandler) -> None:
        self.router = APIRouter(prefix="", tags=["Auth"])
        self.handler = handler
        self.router.add_api_route(
            "/register",
            self.handler.register_user,
            methods=["POST"],
            response_model=BaseResponse[str],
            status_code=status.HTTP_201_CREATED,
            summary="Register with email and password",
            description="Create an inactive account and send an email verification OTP.",
        )
        self.router.add_api_route(
            "/validate",
            self.handler.validate_otp,
            methods=["POST"],
            response_model=BaseResponse[str],
            status_code=status.HTTP_200_OK,
            summary="Validate an email verification OTP",
            description="Activate an inactive account after validating its OTP.",
        )
