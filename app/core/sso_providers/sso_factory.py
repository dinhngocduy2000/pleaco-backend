from uuid import uuid4

from app.common.context import AppContext
from app.common.enum.context_actions import FACEBOOK_AUTHENTICATE, GOOGLE_AUTHENTICATE
from app.common.enum.sso_providers import SSO_PROVIDERS
from app.core.sso_providers.base_sso import BaseSSOStrategy
from app.core.sso_providers.facebook_sso import FacebookSSOStrategy
from app.core.sso_providers.google_sso import GoogleSSOStrategy


class SSOFactory:
    providers: BaseSSOStrategy

    @staticmethod
    def resolve_sso_strategy(provider: SSO_PROVIDERS) -> BaseSSOStrategy:
        if provider == SSO_PROVIDERS.GOOGLE:
            return GoogleSSOStrategy()
        if provider == SSO_PROVIDERS.FACEBOOK:
            return FacebookSSOStrategy()
        raise NotImplementedError("Not Implemented")

    @staticmethod
    def resolve_sso_context(provider: SSO_PROVIDERS) -> AppContext:
        ctx = AppContext(trace_id=uuid4())
        if provider == SSO_PROVIDERS.GOOGLE:
            ctx.action = GOOGLE_AUTHENTICATE
        elif provider == SSO_PROVIDERS.FACEBOOK:
            ctx.action = FACEBOOK_AUTHENTICATE
        return ctx
