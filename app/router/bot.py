from fastapi import APIRouter, status

from app.common.schemas.bot import BotInfo
from app.common.schemas.common import BaseResponse
from app.handler.bot import BotHandler


class BotRouter:
    def __init__(self, handler: BotHandler) -> None:
        self.router = APIRouter(prefix="", tags=["Bots"])
        self.handler = handler
        self.router.add_api_route(
            path="",
            endpoint=self.handler.create_bot,
            methods=["POST"],
            response_model=BaseResponse[BotInfo],
            status_code=status.HTTP_201_CREATED,
            summary="Create a bot",
            description="Create a bot in a group the caller is authorized to manage.",
        )
