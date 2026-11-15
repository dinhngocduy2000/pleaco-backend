from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.common.context import AppContext
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import ForbiddenException
from app.common.schemas.user import Credential
from app.core.database import verify_restricted_runtime_role
from app.core.rbac.permissions import PermissionService


@pytest.mark.asyncio
@pytest.mark.parametrize("active_group", ["different", "none"])
async def test_explicit_group_uses_accepted_target_membership(active_group):
    actor_id, target_group_id = uuid4(), uuid4()
    credential = Credential(
        id=actor_id,
        email="admin@example.com",
        status=UserStatus.ACTIVE,
        active_group_id=uuid4() if active_group == "different" else None,
    )
    member = SimpleNamespace(
        member_id=actor_id,
        group_id=target_group_id,
        role=GroupRole.ADMIN,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    member_repo = SimpleNamespace(get_group_member_by_id=AsyncMock(return_value=member))

    async def transaction_wrapper(callback, context):
        assert context.actor_id == actor_id
        return await callback(SimpleNamespace())

    repo = SimpleNamespace(
        group_members_repo=lambda: member_repo,
        transaction_wrapper=transaction_wrapper,
    )
    service = PermissionService(repo)
    ctx = AppContext(trace_id=uuid4(), actor=actor_id)

    result = await service.get_group_member(credential, ctx, target_group_id)
    assert result.group_id == target_group_id
    assert result.role == GroupRole.ADMIN
    assert member_repo.get_group_member_by_id.await_args.kwargs["group_id"] == target_group_id
    assert member_repo.get_group_member_by_id.await_args.kwargs["accepted_only"] is True

    member_repo.get_group_member_by_id.return_value = None
    with pytest.raises(ForbiddenException):
        await service.get_group_member(credential, ctx, target_group_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_tables", [[], ["maps"], ["robots", "tags"]])
async def test_startup_rejects_unprotected_tables(missing_tables):
    role_result = SimpleNamespace(one=lambda: (False, False, False))
    catalog_result = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: missing_tables)
    )
    connection = SimpleNamespace(execute=AsyncMock(side_effect=[role_result, catalog_result]))

    class ConnectionContext:
        async def __aenter__(self):
            return connection

        async def __aexit__(self, *args):
            pass

    engine = SimpleNamespace(connect=lambda: ConnectionContext())
    if missing_tables:
        with pytest.raises(RuntimeError, match="Tenant row security"):
            await verify_restricted_runtime_role(engine)
    else:
        await verify_restricted_runtime_role(engine)
