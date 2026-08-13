from types import SimpleNamespace
from uuid import uuid4

import bcrypt
import pytest
from pydantic import ValidationError

from app.common.context import AppContext
from app.common.enum.user_status import UserStatus
from app.common.exceptions import BadRequestException
from app.common.schemas.mail import SendMailResponse
from app.common.schemas.user import UserCreate, ValidateOTPRequest
from app.external.queues.queue import TopicMessage
from app.external.queues.topics.user_verification import UserVerificationTopic
from app.models.user import User
from app.services.auth import AuthService


def _ctx() -> AppContext:
    return AppContext(trace_id=uuid4(), action="TEST")


class UserRepositoryStub:
    def __init__(self, user: User | None = None, otp: str | None = None) -> None:
        self.user = user
        self.otp = otp
        self.saved_user: User | None = None
        self.deleted_email: str | None = None

    async def get(self, **kwargs) -> User | None:
        return self.user

    async def save_user(self, session, user: User) -> User:
        self.user = user
        self.saved_user = user
        return user

    async def activate_user(self, session, user: User) -> User:
        user.status = UserStatus.ACTIVE
        return user

    async def set_otp_code(self, email: str, otp: str, **kwargs) -> None:
        self.otp = otp

    async def get_otp_code(self, email: str, **kwargs) -> str | None:
        return self.otp

    async def delete_otp_code(self, email: str, **kwargs) -> None:
        self.deleted_email = email
        self.otp = None


class RegistryStub:
    def __init__(self, user_repo: UserRepositoryStub) -> None:
        self._user_repo = user_repo

    def user_repo(self) -> UserRepositoryStub:
        return self._user_repo

    async def transaction_wrapper(self, callback):
        return await callback(SimpleNamespace())


class VerificationTopicStub:
    def __init__(self, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.messages: list[tuple[str, str]] = []

    async def publish_verification_email(self, email: str, otp: str) -> str:
        if self.should_fail:
            raise RuntimeError("RabbitMQ unavailable")
        self.messages.append((email, otp))
        return "message-id"


def _service(user_repo: UserRepositoryStub, topic: VerificationTopicStub) -> AuthService:
    return AuthService(
        repo=RegistryStub(user_repo),  # type: ignore[arg-type]
        user_service=SimpleNamespace(),  # type: ignore[arg-type]
        mail_service=SimpleNamespace(),  # type: ignore[arg-type]
        verification_topic=topic,  # type: ignore[arg-type]
    )


def test_registration_schema_requires_all_password_character_classes() -> None:
    valid = UserCreate(name="Alex", email="ALEX@example.com", password="Valid!12")
    assert str(valid.email) == "ALEX@example.com"

    for password in ("Shrt1!", "lowercase1!", "UPPERCASE1!", "NoNumber!", "NoSpecial1"):
        with pytest.raises(ValidationError):
            UserCreate(name="Alex", email="alex@example.com", password=password)


@pytest.mark.asyncio
async def test_registration_creates_inactive_user_with_a_fresh_bcrypt_salt() -> None:
    user_repo = UserRepositoryStub()
    topic = VerificationTopicStub()
    service = _service(user_repo, topic)

    await service.create_user(
        UserCreate(name="Alex", email="ALEX@example.com", password="Valid!12"), _ctx()
    )

    assert user_repo.saved_user is not None
    assert user_repo.saved_user.email == "alex@example.com"
    assert user_repo.saved_user.status == UserStatus.INACTIVE
    assert user_repo.saved_user.password != "Valid!12"
    assert bcrypt.checkpw(b"Valid!12", user_repo.saved_user.password.encode())
    assert topic.messages == [("alex@example.com", user_repo.otp)]


@pytest.mark.asyncio
async def test_registration_rejects_active_email_and_resends_for_inactive_email() -> None:
    active_repo = UserRepositoryStub(
        User(name="Alex", email="alex@example.com", password="old", status=UserStatus.ACTIVE)
    )
    with pytest.raises(BadRequestException):
        await _service(active_repo, VerificationTopicStub()).create_user(
            UserCreate(name="Alex", email="alex@example.com", password="Valid!12"), _ctx()
        )

    inactive = User(
        name="Alex", email="alex@example.com", password="old", status=UserStatus.INACTIVE
    )
    inactive_repo = UserRepositoryStub(inactive)
    topic = VerificationTopicStub()
    await _service(inactive_repo, topic).create_user(
        UserCreate(name="Alex Two", email="alex@example.com", password="Changed!2"), _ctx()
    )
    assert inactive.name == "Alex Two"
    assert bcrypt.checkpw(b"Changed!2", inactive.password.encode())
    assert len(topic.messages) == 1


@pytest.mark.asyncio
async def test_queue_failure_does_not_undo_registration() -> None:
    user_repo = UserRepositoryStub()
    await _service(user_repo, VerificationTopicStub(should_fail=True)).create_user(
        UserCreate(name="Alex", email="alex@example.com", password="Valid!12"), _ctx()
    )
    assert user_repo.saved_user is not None
    assert user_repo.saved_user.status == UserStatus.INACTIVE


@pytest.mark.asyncio
async def test_correct_otp_activates_and_consumes_code() -> None:
    user_repo = UserRepositoryStub(
        User(name="Alex", email="alex@example.com", password="hash", status=UserStatus.INACTIVE),
        otp="123456",
    )
    service = _service(user_repo, VerificationTopicStub())

    await service.validate_otp(ValidateOTPRequest(email="ALEX@example.com", otp="123456"), _ctx())

    assert user_repo.user.status == UserStatus.ACTIVE
    assert user_repo.deleted_email == "alex@example.com"


@pytest.mark.asyncio
async def test_invalid_otp_does_not_activate_user() -> None:
    user_repo = UserRepositoryStub(
        User(name="Alex", email="alex@example.com", password="hash", status=UserStatus.INACTIVE),
        otp="123456",
    )
    with pytest.raises(BadRequestException):
        await _service(user_repo, VerificationTopicStub()).validate_otp(
            ValidateOTPRequest(email="alex@example.com", otp="000000"), _ctx()
        )
    assert user_repo.user.status == UserStatus.INACTIVE


@pytest.mark.asyncio
async def test_verification_consumer_renders_and_sends_email() -> None:
    class MailServiceStub:
        def __init__(self) -> None:
            self.request = None

        async def send(self, request, ctx):
            self.request = request
            return SendMailResponse(success=True, message="sent")

    mail_service = MailServiceStub()
    topic = UserVerificationTopic(SimpleNamespace(), mail_service)  # type: ignore[arg-type]
    await topic._handle_message(
        TopicMessage(payload={"email": "alex@example.com", "otp": "123456"}, routing_key="test")
    )
    assert mail_service.request.to == ["alex@example.com"]
    assert "123456" in mail_service.request.html
