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
        raise NotImplementedError("Pleaco-specific implementation is pending.")
