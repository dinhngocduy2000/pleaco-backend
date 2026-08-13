from fastapi import APIRouter
from app.handler.user import UserHandler


class UserRouter:
    router: APIRouter
    handler: UserHandler

    def __init__(self, handler: UserHandler) -> None:
        self.router = APIRouter(prefix="", tags=["Users"], redirect_slashes=False)
        self.handler = handler
