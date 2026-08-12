from typing import Optional, Tuple

from fastapi import Request
from app.common.context import AppContext
from app.common.middleware.logger import Logger
from app.core.sso_providers.base_sso import BaseSSOStrategy

logger = Logger()


class FacebookSSOStrategy(BaseSSOStrategy):
    def get_auth_url(self, ctx: AppContext) -> Tuple[str, str]:
        logger.error("Not Implemented")
        raise NotImplementedError("Not Implemented")

    def callback(self, request: Request, ctx: AppContext) -> Optional[dict]:
        logger.error("Not Implemented")
        raise NotImplementedError("Not Implemented")
