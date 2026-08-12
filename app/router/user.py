from fastapi import APIRouter
from app.handler.user import UserHandler


class UserRouter:
    router: APIRouter
    handler: UserHandler

    def __init__(self, handler: UserHandler) -> None:
        raise NotImplementedError("Pleaco-specific implementation is pending.")
