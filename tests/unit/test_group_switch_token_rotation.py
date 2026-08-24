from datetime import datetime, timedelta, timezone
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import ANY
from uuid import uuid4

import jwt
import pytest
from fastapi import Request
from fastapi.responses import Response

import app.services.auth as auth_service_module
from app.common.context import AppContext
from app.common.enum.user_status import UserStatus
from app.common.exceptions import BadRequestException, UnauthorizedException
from app.common.schemas.user import Credential, SwitchGroupRequest
from app.handler.group import GroupHandler
from app.repository.user import REPLACE_HASHED_TOKEN_SCRIPT, UserRepository
from app.services.auth import AuthService


def _ctx() -> AppContext:
    return AppContext(trace_id=uuid4(), action="SWITCH_CURRENT_USER_GROUP")


def _configure_token_settings(monkeypatch) -> None:
    monkeypatch.setattr(auth_service_module.settings, "SECRET_KEY", "test-secret")
    monkeypatch.setattr(auth_service_module.settings, "ALGORITHM", "HS256")


def _access_token(active_group_id=None, expires_in: int = 120) -> tuple[str, dict]:
    payload = {
        "id": str(uuid4()),
        "email": "alex@example.com",
        "status": UserStatus.ACTIVE.value,
        "is_pending": False,
        "active_group_id": str(active_group_id) if active_group_id else None,
        "token_type": "access",
        "exp_time": "unchanged",
        "exp": int(
            (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).timestamp()
        ),
    }
    return jwt.encode(payload, "test-secret", algorithm="HS256"), payload


class TokenRepositoryStub:
    def __init__(self, replacement_succeeds: bool = True) -> None:
        self.replacement_succeeds = replacement_succeeds
        self.replace_calls: list[dict] = []

    async def replace_hashed_token(self, **kwargs) -> bool:
        self.replace_calls.append(kwargs)
        return self.replacement_succeeds


def _auth_service(token_repository: TokenRepositoryStub) -> AuthService:
    return AuthService(
        repo=SimpleNamespace(user_repo=lambda: token_repository),
        user_service=SimpleNamespace(),
        mail_service=SimpleNamespace(),
        verification_topic=SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_rotation_changes_only_active_group_and_preserves_expiry(
    monkeypatch,
) -> None:
    _configure_token_settings(monkeypatch)
    old_group_id = uuid4()
    new_group_id = uuid4()
    access_token, original_payload = _access_token(old_group_id)
    token_repository = TokenRepositoryStub()

    replacement_token, remaining_ttl = await _auth_service(
        token_repository
    ).rotate_access_token_active_group(access_token, new_group_id, _ctx())

    replacement_payload = jwt.decode(
        replacement_token, "test-secret", algorithms=["HS256"]
    )
    assert replacement_payload["active_group_id"] == str(new_group_id)
    assert replacement_payload["exp"] == original_payload["exp"]
    for claim in {"id", "email", "status", "is_pending", "token_type", "exp_time"}:
        assert replacement_payload[claim] == original_payload[claim]
    assert 1 <= remaining_ttl <= 120
    assert token_repository.replace_calls == [
        {
            "old_hashed_token": sha256(access_token.encode()).hexdigest(),
            "new_hashed_token": sha256(replacement_token.encode()).hexdigest(),
            "expire": remaining_ttl,
            "ctx": ANY,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("token_type", ["refresh", "invalid"])
async def test_rotation_rejects_non_access_token(monkeypatch, token_type: str) -> None:
    _configure_token_settings(monkeypatch)
    access_token, payload = _access_token()
    payload["token_type"] = token_type
    token = jwt.encode(payload, "test-secret", algorithm="HS256")
    token_repository = TokenRepositoryStub()

    with pytest.raises(UnauthorizedException):
        await _auth_service(token_repository).rotate_access_token_active_group(
            token, uuid4(), _ctx()
        )

    assert token_repository.replace_calls == []


@pytest.mark.asyncio
async def test_rotation_rejects_missing_or_expired_access_token(monkeypatch) -> None:
    _configure_token_settings(monkeypatch)
    expired_token, _ = _access_token(expires_in=-1)
    token_repository = TokenRepositoryStub()

    for token in ("", expired_token):
        with pytest.raises(UnauthorizedException):
            await _auth_service(token_repository).rotate_access_token_active_group(
                token, uuid4(), _ctx()
            )

    assert token_repository.replace_calls == []


@pytest.mark.asyncio
async def test_rotation_rejects_a_token_revoked_during_replacement(monkeypatch) -> None:
    _configure_token_settings(monkeypatch)
    access_token, _ = _access_token()
    token_repository = TokenRepositoryStub(replacement_succeeds=False)

    with pytest.raises(UnauthorizedException):
        await _auth_service(token_repository).rotate_access_token_active_group(
            access_token, uuid4(), _ctx()
        )


class RedisClientStub:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[tuple] = []

    async def eval(self, *args):
        self.calls.append(args)
        return self.result


@pytest.mark.asyncio
async def test_repository_replaces_token_hashes_with_one_redis_script(
    monkeypatch,
) -> None:
    monkeypatch.setattr(auth_service_module.settings, "cache_token_hash", "tokens")
    redis_client = RedisClientStub(result=1)
    repository = UserRepository(redis_client)

    replaced = await repository.replace_hashed_token(
        old_hashed_token="old-hash",
        new_hashed_token="new-hash",
        expire=42,
        ctx=_ctx(),
    )

    assert replaced is True
    assert redis_client.calls == [
        (
            REPLACE_HASHED_TOKEN_SCRIPT,
            2,
            "tokens:old-hash",
            "tokens:new-hash",
            "new-hash",
            42,
        )
    ]


class GroupServiceStub:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = []

    async def switch_current_user_active_group(self, input, ctx, credential) -> None:
        self.calls.append((input, ctx, credential))
        if self.error is not None:
            raise self.error


class RotatingAuthServiceStub:
    def __init__(self) -> None:
        self.calls = []

    async def rotate_access_token_active_group(self, **kwargs):
        self.calls.append(kwargs)
        return "replacement-token", 73


def _request_with_access_token(access_token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "PUT",
            "scheme": "https",
            "path": "/groups/switch",
            "raw_path": b"/groups/switch",
            "query_string": b"",
            "headers": [(b"cookie", f"access_token={access_token}".encode())],
        }
    )


@pytest.mark.asyncio
async def test_switch_handler_sets_replacement_access_cookie() -> None:
    group_service = GroupServiceStub()
    auth_service = RotatingAuthServiceStub()
    handler = GroupHandler(group_service, auth_service)
    group_id = uuid4()
    credential = Credential(
        id=uuid4(),
        email="alex@example.com",
        status=UserStatus.ACTIVE,
        active_group_id=uuid4(),
    )
    response = Response()

    result = await handler.switch_current_user_group(
        request=_request_with_access_token("old-token"),
        response=response,
        input=SwitchGroupRequest(group_id=group_id),
        credential=credential,
    )

    assert result == "Success"
    assert auth_service.calls[0]["access_token"] == "old-token"
    assert auth_service.calls[0]["active_group_id"] == group_id
    cookie = response.headers["set-cookie"]
    assert "access_token=replacement-token" in cookie
    assert "Max-Age=73" in cookie
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie


@pytest.mark.asyncio
async def test_failed_group_switch_does_not_rotate_token() -> None:
    group_service = GroupServiceStub(BadRequestException("Group not found"))
    auth_service = RotatingAuthServiceStub()
    handler = GroupHandler(group_service, auth_service)
    response = Response()

    with pytest.raises(BadRequestException):
        await handler.switch_current_user_group(
            request=_request_with_access_token("old-token"),
            response=response,
            input=SwitchGroupRequest(group_id=uuid4()),
            credential=Credential(
                id=uuid4(),
                email="alex@example.com",
                status=UserStatus.ACTIVE,
            ),
        )

    assert auth_service.calls == []
    assert response.headers.get("set-cookie") is None
