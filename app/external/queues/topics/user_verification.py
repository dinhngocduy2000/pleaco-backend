"""RabbitMQ topic for email-verification delivery."""

from datetime import datetime, timezone
from math import ceil
from uuid import uuid4

from pydantic import BaseModel, EmailStr

from app.common.context import AppContext
from app.common.enum.context_actions import SEND_EMAIL
from app.common.middleware.logger import Logger
from app.common.schemas.mail import SendMailRequest
from app.core.config import settings
from app.external.mail.jinja_templates import render_mail_html
from app.external.mail.mail import MailService
from app.external.queues.queue import RabbitMQClient, TopicMessage
from app.external.queues.topics.base import Topic

logger = Logger()


class VerificationEmailMessage(BaseModel):
    """The minimal payload necessary to deliver an email verification message."""

    email: EmailStr
    otp: str


class UserVerificationTopic(Topic):
    """Publishes and consumes account-verification email messages."""

    exchange_name = "pleaco.user"
    routing_key = "user.verification_email_requested"
    queue_name = "pleaco-backend.user-verification-email"

    def __init__(self, client: RabbitMQClient, mail_service: MailService) -> None:
        super().__init__(client, self.exchange_name)
        self._mail_service = mail_service

    async def publish_verification_email(self, email: str, otp: str) -> str:
        return await self.publish(
            self.routing_key,
            VerificationEmailMessage(email=email, otp=otp),
            correlation_id=email,
        )

    async def start_consumer(self) -> None:
        await self.subscribe(
            queue_name=self.queue_name,
            routing_key=self.routing_key,
            handler=self._handle_message,
        )

    async def _handle_message(self, message: TopicMessage) -> None:
        payload = VerificationEmailMessage.model_validate(message.payload)
        ctx = AppContext(trace_id=uuid4(), action=SEND_EMAIL)
        html = render_mail_html(
            "send-otp.html",
            otp=payload.otp,
            expiry_minutes=ceil(settings.OTP_CODE_EXPIRE_SECONDS / 60),
            base_url=settings.email_public_base_url,
            year=datetime.now(timezone.utc).year,
            unique_id=message.message_id or str(uuid4()),
        )
        result = await self._mail_service.send(
            SendMailRequest(
                to=[payload.email],
                subject=f"Verify your {settings.APP_NAME} account",
                body=(
                    f"Your verification code is {payload.otp}. It expires in "
                    f"{ceil(settings.OTP_CODE_EXPIRE_SECONDS / 60)} minutes."
                ),
                html=html,
            ),
            ctx=ctx,
        )
        if not result.success:
            raise RuntimeError(result.message)
        logger.info(msg="Verification email sent", context=ctx)
