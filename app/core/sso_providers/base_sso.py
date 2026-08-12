from abc import ABC, abstractmethod
from typing import Tuple

from fastapi import Request

from app.common.context import AppContext
from app.common.schemas.user import UserLoginResponse


class BaseSSOStrategy(ABC):

    @abstractmethod
    def get_auth_url(self, ctx: AppContext) -> Tuple[str, str]:
        pass

    @abstractmethod
    def callback(self, request: Request, ctx: AppContext) -> UserLoginResponse:
        pass
