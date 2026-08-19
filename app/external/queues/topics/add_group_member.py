"""RabbitMQ topic for group invitation email delivery."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from pydantic import BaseModel, EmailStr

from app.common.context import AppContext
from app.common.enum.context_actions import SEND_EMAIL
from app.common.enum.user_roles import GroupRole
from app.common.middleware.logger import Logger
from app.common.schemas.mail import SendMailRequest
from app.core.config import settings
from app.external.mail.jinja_templates import render_mail_html
from app.external.mail.mail import MailService
from app.external.queues.queue import RabbitMQClient, TopicMessage
from app.external.queues.topics.base import Topic

logger = Logger()


class AddGroupMemberMessage(BaseModel):
    invitation_id: UUID
    email: EmailStr
    group_name: str
    role: GroupRole


class AddGroupMemberTopic(Topic):
    exchange_name = "pleaco.group"
    routing_key = "group.member.invitation_requested"
    queue_name = "pleaco-backend.group-member-invitation-email"

    def __init__(self, client: RabbitMQClient, mail_service: MailService) -> None:
        super().__init__(client, self.exchange_name)
        self._mail_service = mail_service

    async def publish_invitation(self, message: AddGroupMemberMessage) -> str:
        return await self.publish(
            self.routing_key,
            message,
            correlation_id=str(message.invitation_id),
        )

    async def start_consumer(self) -> None:
        await self.subscribe(
            queue_name=self.queue_name,
            routing_key=self.routing_key,
            handler=self._handle_message,
        )

    async def _handle_message(self, message: TopicMessage) -> None:
        payload = AddGroupMemberMessage.model_validate(message.payload)
        ctx = AppContext(trace_id=uuid4(), action=SEND_EMAIL)
        accept_url = f"{settings.FRONTEND_URL.rstrip('/')}/invitation?invitation_id={payload.invitation_id}"
        html = render_mail_html(
            "invite-group-member.html",
            recipient_email=payload.email,
            group_name=payload.group_name,
            role=payload.role.value,
            accept_url=accept_url,
            invitation_id=payload.invitation_id,
            base_url=settings.email_public_base_url,
            year=datetime.now(timezone.utc).year,
        )
        result = await self._mail_service.send(
            SendMailRequest(
                to=[str(payload.email)],
                subject=f"Invitation to join {payload.group_name}",
                body=(
                    f"{payload.email}, you have been invited to join "
                    f"{payload.group_name} as a {payload.role.value}. "
                    f"Accept: {accept_url}. Invitation ID: {payload.invitation_id}"
                ),
                html=html,
            ),
            ctx=ctx,
        )
        if not result.success:
            raise RuntimeError(result.message)
        logger.info(msg="Group invitation email sent", context=ctx)
