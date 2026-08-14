import secrets
from typing import Any, Tuple
from urllib.parse import urlencode
from fastapi import Request
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
import httpx
from app.common.context import AppContext
from app.common.exceptions import BadRequestException
from app.common.middleware.logger import Logger
from app.core.config import settings
from app.core.sso_providers.base_sso import BaseSSOStrategy

logger = Logger()


class GoogleSSOStrategy(BaseSSOStrategy):
    state_cookie_name = "google_oauth_state"

    def get_auth_url(self, ctx: AppContext) -> Tuple[str, str]:
        """
        Build the Google OAuth 2.0 authorization URL for redirect-based sign-in.
        Returns (url, state). The handler should set state in a cookie and return the URL to the frontend.
        """
        if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_REDIRECT_URI:
            logger.error(
                msg="Google OAuth URL is not configured (GOOGLE_CLIENT_ID, GOOGLE_REDIRECT_URI)",
                context=ctx,
            )
            raise BadRequestException(message="Google Sign-In is not configured")
        state = secrets.token_urlsafe(32)
        params = {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": settings.GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
        return url, state

    async def callback(self, request: Request, ctx: AppContext) -> dict[str, Any]:
        """
        Exchange Google authorization code for tokens, then get or create user and return our JWTs.
        Validates state against the cookie set when the auth URL was requested.
        """
        if not all(
            [
                settings.GOOGLE_CLIENT_ID,
                settings.GOOGLE_CLIENT_SECRET,
                settings.GOOGLE_REDIRECT_URI,
            ]
        ):
            logger.error(
                msg="Google callback is not configured",
                context=ctx,
            )
            raise BadRequestException(message="Google Sign-In is not configured")

        state_cookie = request.cookies.get(self.state_cookie_name)
        state = request.query_params.get("state")
        code = request.query_params.get("code")

        if not state or not state_cookie or not secrets.compare_digest(state, state_cookie):
            logger.error(msg="Invalid or missing state in Google callback", context=ctx)
            raise BadRequestException(message="Invalid state")
        if not code:
            logger.error(msg="Missing authorization code in Google callback", context=ctx)
            raise BadRequestException(message="Google sign-in failed")

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                    "grant_type": "authorization_code",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code != 200:
            logger.error(
                msg=f"Google token exchange failed with status {resp.status_code}",
                context=ctx,
            )
            raise BadRequestException(message="Google sign-in failed")

        data = resp.json()
        id_token_str = data.get("id_token")
        if not id_token_str:
            logger.error(msg="Google response missing id_token", context=ctx)
            raise BadRequestException(message="Google sign-in failed")

        try:
            idinfo = google_id_token.verify_oauth2_token(
                id_token_str,
                google_requests.Request(),
                settings.GOOGLE_CLIENT_ID,
            )
        except ValueError:
            logger.error(
                msg="Invalid Google ID token from callback",
                context=ctx,
            )
            raise BadRequestException(message="Google sign-in failed")

        if not idinfo.get("email") or idinfo.get("email_verified") is not True:
            logger.error(msg="Google account email is not verified", context=ctx)
            raise BadRequestException(message="Google account email is not verified")
        return idinfo
