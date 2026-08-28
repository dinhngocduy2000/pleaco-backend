from fastapi import APIRouter, status

from app.common.schemas.bot import BotInfo, BotKeyValueInfo, BotListInfo
from app.common.schemas.common import (
    BaseResponse,
    PaginationBaseResponse,
)
from app.handler.bot import BotHandler


class BotRouter:
    def __init__(self, handler: BotHandler) -> None:
        self.router = APIRouter(prefix="", tags=["Bots"])
        self.handler = handler
        self.router.add_api_route(
            path="",
            endpoint=self.handler.list_bots,
            methods=["GET"],
            response_model=PaginationBaseResponse[BotListInfo],
            status_code=status.HTTP_200_OK,
            summary="List group bots",
            description="List bots for a group the caller is an accepted member of.",
        )
        self.router.add_api_route(
            path="",
            endpoint=self.handler.create_bot,
            methods=["POST"],
            response_model=BaseResponse[BotInfo],
            status_code=status.HTTP_201_CREATED,
            summary="Create a bot",
            description="Create a bot in a group the caller is authorized to manage.",
        )
        self.router.add_api_route(
            path="/key-value",
            endpoint=self.handler.list_bot_key_value,
            methods=["GET"],
            response_model=BaseResponse[list[BotKeyValueInfo]],
            status_code=status.HTTP_200_OK,
            summary="List active-group bot options",
            description=(
                "List all bots in the caller's active group as ID/name pairs, "
                "optionally searched by name or serial number."
            ),
        )
        self.router.add_api_route(
            path="/{bot_id}",
            endpoint=self.handler.delete_bot,
            methods=["DELETE"],
            status_code=status.HTTP_204_NO_CONTENT,
            summary="Delete a bot",
            description="Hard-delete a non-executing bot in the caller's active group.",
        )
