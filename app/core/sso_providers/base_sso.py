from abc import ABC, abstractmethod
from typing import Any, Tuple

from fastapi import Request

from app.common.context import AppContext
class BaseSSOStrategy(ABC):
    state_cookie_name: str

    @abstractmethod
    def get_auth_url(self, ctx: AppContext) -> Tuple[str, str]:
        pass

    @abstractmethod
    async def callback(self, request: Request, ctx: AppContext) -> dict[str, Any]:
        pass
