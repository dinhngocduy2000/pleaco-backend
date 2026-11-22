import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.common.exceptions import BadRequestException
from app.services.map import MapService


@pytest.mark.asyncio
async def test_missing_position_uses_assigned_dock_centroid():
    group, map_id, docked, undocked, invalid = (uuid4() for _ in range(5))
    updated = datetime(2026, 1, 1, tzinfo=timezone.utc)
    records = [(docked, 'A', 1.5, 2.5, updated), (undocked, 'B', None, None, None),
               (invalid, 'C', 3.0, 4.0, updated)]
    repository = SimpleNamespace(get_position_robots=AsyncMock(return_value=records))
    async def transaction(callback, context):
        return await callback(None)
    repo = SimpleNamespace(map_repo=lambda: repository, transaction_wrapper=transaction)
    redis = SimpleNamespace(mget=AsyncMock(return_value=[None, None, 'invalid']))
    result = await MapService.get_robot_positions.__wrapped__(
        MapService(repo, None, redis_client=redis), map_id, group,
        SimpleNamespace(id=uuid4(), active_group_id=group), SimpleNamespace())
    assert result.robots[0].position.model_dump() == {'x': 1.5, 'y': 2.5, 'updated_at': updated, 'source': 'dock'}
    assert result.robots[1].position is None
    assert result.robots[2].position is None


@pytest.mark.asyncio
async def test_positions_validate_identity_and_keep_unknown():
    group, map_id, robot, other = (uuid4() for _ in range(4))
    dock_updated = datetime(2026, 1, 1, tzinfo=timezone.utc)
    records = [(robot, 'A', 8, 9, dock_updated), (other, 'B', 8, 9, dock_updated)]
    repo = SimpleNamespace(map_repo=lambda: SimpleNamespace(get_position_robots=AsyncMock(return_value=records)))
    async def transaction(callback, context):
        return await callback(None)
    repo.transaction_wrapper = transaction
    valid = dict(robot_id=str(robot), group_id=str(group), map_id=str(map_id), command_id=str(uuid4()), x=1, y=2, updated_at='2026-01-01T00:00:00Z')
    invalid = dict(valid, robot_id=str(robot))
    redis = SimpleNamespace(mget=AsyncMock(return_value=[json.dumps(valid), json.dumps(invalid)]))
    service = MapService(repo, None, redis_client=redis)
    result = await MapService.get_robot_positions.__wrapped__(service, map_id, group, SimpleNamespace(id=uuid4(), active_group_id=group), SimpleNamespace())
    assert result.robots[0].position.x == 1
    assert result.robots[0].position.source == "confirmed"
    assert result.robots[1].position is None
    redis.mget.assert_awaited_once()


@pytest.mark.asyncio
async def test_redis_failure_is_unavailable():
    group, map_id = uuid4(), uuid4()
    repo = SimpleNamespace(map_repo=lambda: SimpleNamespace(get_position_robots=AsyncMock(return_value=[])))
    async def transaction(callback, context):
        return await callback(None)
    repo.transaction_wrapper = transaction
    service = MapService(repo, None, redis_client=SimpleNamespace(mget=AsyncMock(side_effect=OSError('offline'))))
    with pytest.raises(BadRequestException) as error:
        await MapService.get_robot_positions.__wrapped__(service, map_id, group, SimpleNamespace(id=uuid4(), active_group_id=group), SimpleNamespace())
    assert error.value.status_code == 503

@pytest.mark.asyncio
async def test_guest_can_read_empty_map_and_other_group_map_is_hidden():
    from app.common.context import AppContext
    from app.common.enum.context_actions import LIST_MAPS
    from app.common.enum.user_roles import GroupRole
    from app.common.exceptions import NotFoundException

    group, map_id, foreign_map, actor = (uuid4() for _ in range(4))
    async def lookup(session, requested_map, requested_group, ctx):
        assert requested_group == group
        return [] if requested_map == map_id else None
    repository = SimpleNamespace(get_position_robots=AsyncMock(side_effect=lookup))
    async def transaction(callback, context):
        assert context.group_id == group
        return await callback(None)
    registry = SimpleNamespace(map_repo=lambda: repository, transaction_wrapper=transaction)
    permissions = SimpleNamespace(get_group_member=AsyncMock(return_value=SimpleNamespace(role=GroupRole.GUEST)), is_action_executable=lambda **kwargs: True)
    redis = SimpleNamespace(mget=AsyncMock(return_value=[]))
    service = MapService(registry, permissions, redis_client=redis)
    credential = SimpleNamespace(id=actor, active_group_id=group)
    ctx = AppContext(trace_id=uuid4(), action=LIST_MAPS, actor=actor)
    response = await service.get_robot_positions(map_id=map_id, group_id=group, credential=credential, ctx=ctx)
    assert response.robots == []
    with pytest.raises(NotFoundException):
        await service.get_robot_positions(map_id=foreign_map, group_id=group, credential=credential, ctx=ctx)
    redis.mget.assert_awaited_once_with([])


@pytest.mark.asyncio
async def test_position_query_scopes_both_map_and_robot_to_group():
    from sqlalchemy.dialects import postgresql
    from app.repository.map import MapRepository

    result = SimpleNamespace(all=lambda: [])
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    map_id, group_id = uuid4(), uuid4()
    assert await MapRepository().get_position_robots(session, map_id, group_id, None) is None
    query = session.execute.await_args.args[0]
    compiled = query.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert 'maps.group_id' in sql and 'robots.group_id' in sql
    assert 'docking_station.map_id = maps.id' in sql
    assert list(compiled.params.values()).count(group_id) == 2
