import aiosmtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.common.context import AppContext
from app.common.middleware.logger import Logger
from app.common.schemas.mail import SendMailRequest, SendMailResponse
from app.core.config import settings
from app.core.notifications.base_notification import BaseNotificationChannels

logger = Logger()


class MailService(BaseNotificationChannels[SendMailRequest, SendMailResponse]):
    """
    SMTP-based email service.

    To swap providers (e.g., SendGrid, SES), create a new subclass of
    BaseNotificationChannels[SendMailRequest, SendMailResponse] and inject it
    instead of this class in main.py.
    """

    def __init__(self) -> None:
        self.host = settings.SMTP_HOST
        self.port = settings.SMTP_PORT
        self.username = settings.SMTP_USERNAME
        self.password = settings.SMTP_PASSWORD
        self.from_email = settings.SMTP_FROM_EMAIL
        self.use_tls = settings.SMTP_USE_TLS

    def _build_message(
        self, request: SendMailRequest, ctx: AppContext
    ) -> MIMEMultipart:
        msg = MIMEMultipart("alternative")
        msg["From"] = self.from_email
        msg["To"] = ", ".join(request.to)
        msg["Subject"] = request.subject

        if request.cc:
            msg["Cc"] = ", ".join(request.cc)

        msg.attach(MIMEText(request.body, "plain"))

        if request.html:
            msg.attach(MIMEText(request.html, "html"))

        return msg

    def _all_recipients(self, request: SendMailRequest) -> list[str]:
        recipients = list(request.to)
        if request.cc:
            recipients.extend(request.cc)
        if request.bcc:
            recipients.extend(request.bcc)
        return recipients

    def push(self):
        pass

    async def send(self, request: SendMailRequest, ctx: AppContext) -> SendMailResponse:
        logger.info(
            msg=f"Sending email to {request.to} with subject '{request.subject}'",
            context=ctx,
        )

        message = self._build_message(request, ctx=ctx)
        recipients = self._all_recipients(request)

        implicit_tls = self.use_tls and self.port == 465
        start_tls = self.use_tls and not implicit_tls
        smtp = aiosmtplib.SMTP(
            hostname=self.host,
            port=self.port,
            use_tls=implicit_tls,
            start_tls=start_tls,
        )

        try:
            await smtp.connect()

            if self.username and self.password:
                await smtp.login(self.username, self.password)

            await smtp.sendmail(self.from_email, recipients, message.as_string())
            await smtp.quit()

            logger.info(msg="Email sent successfully", context=ctx)
            return SendMailResponse(success=True, message="Email sent successfully")
        except aiosmtplib.SMTPAuthenticationError as e:
            logger.error(msg=f"SMTP authentication failed: {e}", context=ctx)
            return SendMailResponse(
                success=False, message="SMTP authentication failed — check credentials"
            )
        except aiosmtplib.SMTPException as e:
            logger.error(msg=f"SMTP error while sending email: {e}", context=ctx)
            return SendMailResponse(success=False, message=f"Failed to send email: {e}")
        except Exception as e:
            logger.error(msg=f"Unexpected error while sending email: {e}", context=ctx)
            return SendMailResponse(
                success=False,
                message="An unexpected error occurred while sending email",
            )
