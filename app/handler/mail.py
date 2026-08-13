from datetime import datetime
from uuid import uuid4

from fastapi import Depends

from app.common.context import AppContext
from app.common.enum.context_actions import SEND_EMAIL
from app.common.exceptions import BadRequestException
from app.common.exceptions.decorator import exception_handler
from app.common.middleware.auth_middleware import AuthMiddleware
from app.common.middleware.logger import Logger
from app.common.schemas.mail import SendMailRequest, SendMailResponse
from app.common.schemas.user import Credential
from app.common.utils.generate_otp import generate_otp
from app.core.notifications.base_notification import BaseNotificationChannels
from app.external.mail.jinja_templates import render_mail_html
from app.core.config import settings

logger = Logger()


class MailHandler:
    service: BaseNotificationChannels[SendMailRequest, SendMailResponse]

    def __init__(
        self, service: BaseNotificationChannels[SendMailRequest, SendMailResponse]
    ) -> None:
        self.service = service

    @exception_handler
    async def send_email(
        self,
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> SendMailResponse:
        raise NotImplementedError("Pleaco-specific implementation is pending.")
