from fastapi import APIRouter, status
from fastapi.responses import RedirectResponse

from app.common.schemas.common import BaseResponse
from app.common.schemas.user import (
    SSOLoginResponse,
    UserInfo,
    UserLoginResponse,
)
from app.handler.auth import AuthHandler


class AuthRouter:
    router: APIRouter
    handler: AuthHandler

    def __init__(self, handler: AuthHandler) -> None:
        self.router = APIRouter(prefix="", tags=["Auth"])
        self.handler = handler
        self.router.add_api_route(
            "/login",
            self.handler.authenticate_user,
            methods=["POST"],
            response_model=UserLoginResponse,
            status_code=status.HTTP_200_OK,
            summary="Login with email and password",
            description="Authenticate an active account and issue access and refresh tokens.",
        )
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

        self.router.add_api_route(
            "/logout",
            self.handler.logout,
            methods=["POST"],
            response_model=str,
            status_code=status.HTTP_200_OK,
            summary="Logout user",
            description="Invalidate the access and refresh tokens.",
        )

        self.router.add_api_route(
            path="/sso",
            endpoint=self.handler.get_sso_auth_url,
            methods=["GET"],
            response_model=SSOLoginResponse,
            status_code=status.HTTP_200_OK,
            summary="Get SSO sign-in URL",
            description="Returns the SSO OAuth authorization URL. Frontend should redirect the user to this URL to start sign-in. A state cookie is set for validation at the callback.",
            response_description="Object with url to redirect the user to.",
            responses={
                200: {
                    "description": "SSO auth URL",
                    "content": {
                        "application/json": {
                            "example": {
                                "url": "https://accounts.google.com/o/oauth2/v2/auth?..."
                            }
                        }
                    },
                },
                400: {
                    "description": "SSO Sign-In not configured",
                },
            },
        )

        self.router.add_api_route(
            path="/sso/callback",
            endpoint=self.handler.google_callback,
            methods=["GET"],
            response_class=RedirectResponse,
            status_code=status.HTTP_302_FOUND,
            summary="SSO OAuth callback",
            description="Called by Google after user signs in. Exchanges the code for tokens, creates session, redirects to the frontend URL.",
            responses={
                302: {"description": "Redirect to frontend with session cookies set"},
                400: {"description": "Invalid state or token exchange failed"},
            },
        )

        self.router.add_api_route(
            path="/refresh",
            endpoint=self.handler.refresh_token,
            methods=["POST"],
            response_model=str,
            status_code=status.HTTP_200_OK,
            summary="Refresh a token",
            description="Refresh a token. ",
            response_description="The refreshed token information",
            responses={
                200: {
                    "description": "Token refreshed successfully",
                    "content": {
                        "application/json": {
                            "example": {"detail": "Successfully refreshed token"}
                        }
                    },
                },
                400: {
                    "description": "Bad request - Invalid refresh token",
                    "content": {
                        "application/json": {
                            "example": {"detail": "Invalid refresh token"}
                        }
                    },
                },
            },
        )

        self.router.add_api_route(
            path="/profile",
            endpoint=self.handler.get_current_user_profile,
            methods=["GET"],
            response_model=BaseResponse[UserInfo],
            status_code=status.HTTP_200_OK,
            summary="Get current user profile",
            description="Get current user profile based on the user id in the credential",
            response_description="The current user profile information",
            responses={
                200: {
                    "description": "User profile retrieved successfully",
                    "content": {
                        "application/json": {
                            "example": {
                                "data": {
                                    "id": "550e8400-e29b-41d4-a716-446655440000",
                                    "name": "John Doe",
                                    "email": "john@example.com",
                                    "status": "active",
                                    "created_at": "2026-01-25T10:30:00Z",
                                    "updated_at": "2026-01-25T10:30:00Z",
                                    "image_url": None,
                                    "active_group_id": "550e8400-e29b-41d4-a716-446655440000",
                                },
                                "message": "Success",
                                "statusCode": 200,
                            }
                        }
                    },
                },
            },
        )