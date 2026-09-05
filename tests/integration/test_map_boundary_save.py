"""Spatial save tests; requires MAP_BOUNDARY_TEST_DATABASE_URL on disposable PostGIS."""

import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.common.context import AppContext
from app.common.enum.context_actions import SAVE_MAP_BOUNDARY
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import BadRequestException, NotFoundException
from app.common.schemas.map import MapBoundarySaveDTO
from app.common.schemas.user import Credential
from app.core.database import Base
from app.core.rbac.permissions import PermissionService
from app.models import Group, Map, MapBoundary, User
from app.repository.map import MapRepository
from app.repository.map_boundary import MapBoundaryRepository
from app.repository.registry import Registry
from app.services.map import MapService
from tests.integration.test_map_boundaries import db  # noqa: F401


def credential(group_id):
    return Credential(id=uuid4(), email="spatial@example.com", status=UserStatus.ACTIVE,
                      active_group_id=group_id)


def permissions():
    async def member(**kwargs):
        return SimpleNamespace(role=GroupRole.ADMIN)
    return SimpleNamespace(get_group_member=member,
                           is_action_executable=PermissionService.is_action_executable)


def service_for_connection(connection):
    async def transaction(callback):
        async with AsyncSession(bind=connection, join_transaction_mode="create_savepoint") as session:
            async with session.begin():
                return await callback(session)
    return MapService(SimpleNamespace(
        map_repo=MapRepository, map_boundary_repo=MapBoundaryRepository,
        transaction_wrapper=transaction,
    ), permissions())


async def save(service, actor, map_id, source="DIMENSIONS", coordinates=None):
    payload = {"map_id": map_id, "source": source}
    if coordinates is not None:
        payload["geometry"] = {"type": "Polygon", "coordinates": coordinates}
    return await service.save_boundary(
        boundary_save=MapBoundarySaveDTO(**payload), group_id=actor.active_group_id,
        credential=actor, ctx=AppContext(trace_id=uuid4(), action=SAVE_MAP_BOUNDARY, actor=actor.id),
    )


@pytest.mark.asyncio
async def test_create_replace_preserves_identity_and_local_geometry(db):
    connection, map_id = db
    group_id = await connection.scalar(select(Map.group_id).where(Map.id == map_id))
    actor, service = credential(group_id), service_for_connection(connection)
    await connection.execute(Map.__table__.update().values(dimension_x=10.25, dimension_y=5.5))
    first = await save(service, actor, map_id)
    assert first.geometry.coordinates == [[(0, 0), (10.25, 0), (10.25, 5.5), (0, 5.5), (0, 0)]]
    holes = [
        [[0, 0], [10.25, 0], [10.25, 5.5], [0, 5.5], [0, 0]],
        [[1, 1], [1, 2], [2, 2], [2, 1], [1, 1]],
    ]
    for source in ["CUSTOM", "TEACH_MODE", "DIMENSIONS"]:
        await connection.execute(text("UPDATE map_boundaries SET updated_at = '2000-01-01'::timestamptz"))
        result = await save(service, actor, map_id, source, holes)
        assert result.id == first.id and result.created_at == first.created_at
        assert result.updated_at.year > 2000
        assert result.source.value == source
        if source != "DIMENSIONS":
            assert result.geometry.model_dump(mode="json")["coordinates"] == holes
    assert await connection.scalar(select(func.count()).select_from(MapBoundary)) == 1
    assert await connection.scalar(select(func.ST_SRID(MapBoundary.geometry))) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("coordinates", [
    [[[0, 0], [4, 4], [0, 4], [4, 0], [0, 0]]],  # self-intersection
    [[[0, 0], [0, 0], [0, 0], [0, 0]]],  # zero area
    [[[-1, 0], [4, 0], [4, 4], [-1, 0]]],  # outside map
    [[[0, 0], [11, 0], [11, 4], [0, 0]]],
    [[[0, 0], [5, 0], [5, 5], [0, 0]], [[7, 7], [8, 7], [8, 8], [7, 7]]],
])
async def test_invalid_replacement_rolls_back(db, coordinates):
    connection, map_id = db
    actor = credential(await connection.scalar(select(Map.group_id).where(Map.id == map_id)))
    service = service_for_connection(connection)
    original = await save(service, actor, map_id)
    before = (await connection.execute(select(
        MapBoundary.id, MapBoundary.source, MapBoundary.updated_at,
        func.ST_AsEWKB(MapBoundary.geometry),
    ))).one()
    with pytest.raises(BadRequestException):
        await save(service, actor, map_id, "CUSTOM", coordinates)
    after = (await connection.execute(select(
        MapBoundary.id, MapBoundary.source, MapBoundary.updated_at,
        func.ST_AsEWKB(MapBoundary.geometry),
    ))).one()
    assert before == after and after.id == original.id


@pytest.mark.asyncio
async def test_cross_group_and_missing_map_return_not_found(db):
    connection, map_id = db
    service = service_for_connection(connection)
    actor = credential(uuid4())
    for target in [map_id, uuid4()]:
        with pytest.raises(NotFoundException, match="Map not found"):
            await save(service, actor, target)
    assert await connection.scalar(select(func.count()).select_from(MapBoundary)) == 0


@pytest.mark.asyncio
async def test_concurrent_first_writes_serialize_and_keep_one_identity():
    url = os.environ.get("MAP_BOUNDARY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires a disposable PostGIS database")
    schema = "boundary_concurrency_" + uuid4().hex
    admin_engine = create_async_engine(url)
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": f"{schema},public"}})
    tasks = []
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS postgis WITH SCHEMA public"))
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public"))
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        user_id, group_id, map_id = uuid4(), uuid4(), uuid4()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(User.__table__.insert().values(id=user_id, name="Test", email="concurrent@example.com"))
            await connection.execute(Group.__table__.insert().values(id=group_id, owner_id=user_id, name="Test"))
            await connection.execute(Map.__table__.insert().values(
                id=map_id, group_id=group_id, name="Test", dimension_x=10, dimension_y=10,
            ))
        locked, release, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class FirstMapRepository(MapRepository):
            async def get_by_id_and_group_for_update(self, *args, **kwargs):
                result = await super().get_by_id_and_group_for_update(*args, **kwargs)
                locked.set()
                await asyncio.wait_for(release.wait(), 10)
                return result

        class SecondMapRepository(MapRepository):
            async def get_by_id_and_group_for_update(self, *args, **kwargs):
                second_started.set()
                return await super().get_by_id_and_group_for_update(*args, **kwargs)

        first_registry, second_registry = Registry(engine, None), Registry(engine, None)
        first_registry._map_repo = FirstMapRepository()
        second_registry._map_repo = SecondMapRepository()
        actor = credential(group_id)
        first_service = MapService(first_registry, permissions())
        second_service = MapService(second_registry, permissions())
        tasks.append(asyncio.create_task(save(first_service, actor, map_id)))
        await asyncio.wait_for(locked.wait(), 10)
        polygon = [[[0, 0], [2, 0], [2, 2], [0, 0]]]
        tasks.append(asyncio.create_task(save(second_service, actor, map_id, "CUSTOM", polygon)))
        await asyncio.wait_for(second_started.wait(), 10)
        assert not tasks[1].done()
        release.set()
        first, second = await asyncio.wait_for(asyncio.gather(*tasks), 15)
        assert first.id == second.id and first.created_at == second.created_at
        async with engine.connect() as connection:
            assert await connection.scalar(select(func.count()).select_from(MapBoundary)) == 1
            assert (await connection.scalar(select(MapBoundary.source))).value == "CUSTOM"
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()
