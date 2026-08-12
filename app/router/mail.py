from fastapi import APIRouter, status

from app.common.schemas.mail import SendMailResponse
from app.handler.mail import MailHandler


class MailRouter:
    router: APIRouter
    handler: MailHandler

    def __init__(self, handler: MailHandler) -> None:
        raise NotImplementedError("Pleaco-specific implementation is pending.")
