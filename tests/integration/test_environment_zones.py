"""PostGIS integration tests for environment-zone persistence.

Set MAP_BOUNDARY_TEST_DATABASE_URL to a disposable PostgreSQL/PostGIS database.
Application configuration must be supplied without loading protected env files.
"""

import asyncio
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from geoalchemy2 import WKTElement
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import selectinload

from app.common.enum.environment_zone import EnvironmentZoneType
from app.common.enum.context_actions import CREATE_ENVIRONMENT_ZONES
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import BadRequestException
from app.common.schemas.map import EnvironmentZonesCreateDTO
from app.common.schemas.user import Credential
from app.common.context import AppContext
from app.core.database import Base
from app.core.rbac.permissions import PermissionService
from app.models import EnvironmentZone, Group, Map, MapBoundary, User
from app.repository.environment_zone import EnvironmentZoneRepository
from app.repository.map import MapRepository
from app.repository.registry import Registry
from app.services.map import MapService


POLYGON = "POLYGON((0 0,10 0,10 10,0 10,0 0))"
MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic/versions/0017_create_environment_zones.py"
)
spec = importlib.util.spec_from_file_location(
    "environment_zone_migration", MIGRATION_PATH
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def run_migration(connection, direction):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration, direction)()


def actor(group_id):
    return Credential(
        id=uuid4(),
        email="environment-zone-service@example.com",
        status=UserStatus.ACTIVE,
        active_group_id=group_id,
    )


def permissions():
    async def member(**kwargs):
        return SimpleNamespace(role=GroupRole.ADMIN)

    return SimpleNamespace(
        get_group_member=member,
        is_action_executable=PermissionService.is_action_executable,
    )


def service_for_connection(connection):
    async def transaction(callback):
        async with AsyncSession(
            bind=connection, join_transaction_mode="create_savepoint"
        ) as session:
            async with session.begin():
                return await callback(session)

    return MapService(
        SimpleNamespace(
            map_repo=MapRepository,
            environment_zone_repo=EnvironmentZoneRepository,
            transaction_wrapper=transaction,
        ),
        permissions(),
    )


def rectangle(left, bottom, right, top):
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [left, bottom],
                [right, bottom],
                [right, top],
                [left, top],
                [left, bottom],
            ]
        ],
    }


async def create_zones(service, credential, map_id, geometries):
    request = EnvironmentZonesCreateDTO.model_validate(
        {
            "map_id": map_id,
            "zones": [
                {"type": zone_type.value, "geometry": geometry}
                for zone_type, geometry in geometries
            ],
        }
    )
    return await service.create_environment_zones(
        zones_create=request,
        group_id=credential.active_group_id,
        credential=credential,
        ctx=AppContext(
            trace_id=uuid4(),
            action=CREATE_ENVIRONMENT_ZONES,
            actor=credential.id,
        ),
    )


@pytest_asyncio.fixture
async def db():
    url = os.environ.get("MAP_BOUNDARY_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "MAP_BOUNDARY_TEST_DATABASE_URL requires a disposable PostGIS database"
        )
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                schema = "environment_zone_test_" + uuid4().hex
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
                await connection.execute(
                    text(f'SET LOCAL search_path TO "{schema}", public')
                )
                await connection.execute(
                    text("CREATE EXTENSION IF NOT EXISTS postgis WITH SCHEMA public")
                )
                await connection.execute(
                    text("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
                )
                await connection.run_sync(
                    lambda sync: Base.metadata.create_all(
                        sync,
                        tables=[
                            table
                            for table in Base.metadata.sorted_tables
                            if table.name != "environment_zones"
                        ],
                    )
                )
                await connection.run_sync(run_migration, "upgrade")
                user_id, group_id, map_id = uuid4(), uuid4(), uuid4()
                await connection.execute(
                    User.__table__.insert().values(
                        id=user_id,
                        name="Environment zone test",
                        email="environment-zone@example.com",
                    )
                )
                await connection.execute(
                    Group.__table__.insert().values(
                        id=group_id,
                        name="Environment zone test",
                        owner_id=user_id,
                    )
                )
                await connection.execute(
                    Map.__table__.insert().values(
                        id=map_id,
                        group_id=group_id,
                        name="Environment zone test",
                        dimension_x=10,
                        dimension_y=10,
                    )
                )
                yield connection, map_id
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_contract_and_downgrade(db):
    connection, _ = db
    enum_values = (
        (
            await connection.execute(
                text(
                    "SELECT enumlabel FROM pg_enum "
                    "JOIN pg_type ON pg_type.oid = pg_enum.enumtypid "
                    "WHERE pg_type.typname = 'environment_zone_type' "
                    "ORDER BY enumsortorder"
                )
            )
        )
        .scalars()
        .all()
    )
    assert enum_values == ["NO_GO", "OBSTACLE", "CLEANING_ZONE"]

    indexes = (
        await connection.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = current_schema() "
                "AND tablename = 'environment_zones'"
            )
        )
    ).all()
    assert {row.indexname for row in indexes} == {
        "environment_zones_pkey",
        "ix_environment_zones_map_id",
    }
    assert not any("gist" in row.indexdef.lower() for row in indexes)

    constraints = (
        (
            await connection.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'environment_zones'::regclass"
                )
            )
        )
        .scalars()
        .all()
    )
    assert {
        "ck_environment_zones_geometry_valid",
        "ck_environment_zones_geometry_not_empty",
        "ck_environment_zones_geometry_srid",
    }.issubset(constraints)

    await connection.run_sync(run_migration, "downgrade")
    assert (
        await connection.scalar(text("SELECT to_regclass('environment_zones')")) is None
    )
    assert (
        await connection.scalar(text("SELECT to_regtype('environment_zone_type')"))
        is None
    )
    assert await connection.scalar(select(func.count()).select_from(Map)) == 1
    assert (
        await connection.scalar(
            text("SELECT count(*) FROM pg_extension WHERE extname = 'postgis'")
        )
        == 1
    )


@pytest.mark.asyncio
async def test_orm_relationship_types_timestamps_and_orphan_deletion(db):
    connection, map_id = db
    async with AsyncSession(bind=connection, expire_on_commit=False) as session:
        map_record = await session.scalar(
            select(Map).options(selectinload(Map.environment_zones))
        )
        zones = [
            EnvironmentZone(type=zone_type, geometry=WKTElement(POLYGON, srid=0))
            for zone_type in EnvironmentZoneType
        ]
        map_record.environment_zones.extend(zones)
        await session.flush()

        assert all(zone.id is not None and zone.map_id == map_id for zone in zones)
        assert all(zone.map is map_record for zone in zones)
        assert (
            await session.scalar(select(func.count()).select_from(EnvironmentZone)) == 3
        )
        assert set((await session.scalars(select(EnvironmentZone.type))).all()) == set(
            EnvironmentZoneType
        )

        zone = zones[0]
        await session.refresh(zone)
        assert zone.geometry.srid == 0
        await connection.execute(
            EnvironmentZone.__table__.update()
            .where(EnvironmentZone.id == zone.id)
            .values(geometry=zone.geometry)
        )
        assert (
            await connection.scalar(
                select(func.ST_SRID(EnvironmentZone.geometry)).where(
                    EnvironmentZone.id == zone.id
                )
            )
            == 0
        )

        await connection.execute(
            EnvironmentZone.__table__.update()
            .where(EnvironmentZone.id == zone.id)
            .values(updated_at=text("'2000-01-01'::timestamptz"))
        )
        await session.refresh(zone)
        previous = zone.updated_at
        zone.type = EnvironmentZoneType.OBSTACLE
        await session.flush()
        await session.refresh(zone)
        assert zone.created_at.tzinfo is not None
        assert zone.updated_at > previous

        map_record.environment_zones.remove(zones[-1])
        await session.flush()
        assert (
            await session.scalar(select(func.count()).select_from(EnvironmentZone)) == 2
        )


@pytest.mark.asyncio
async def test_polygon_holes_and_database_cascade(db):
    connection, map_id = db
    geometry = "POLYGON((0 0,10 0,10 10,0 10,0 0),(2 2,2 4,4 4,4 2,2 2))"
    await connection.execute(
        EnvironmentZone.__table__.insert().values(
            map_id=map_id,
            type=EnvironmentZoneType.CLEANING_ZONE,
            geometry=WKTElement(geometry, srid=0),
        )
    )
    assert (
        await connection.scalar(select(func.ST_AsText(EnvironmentZone.geometry)))
        == geometry
    )
    await connection.execute(Map.__table__.delete().where(Map.id == map_id))
    assert (
        await connection.scalar(select(func.count()).select_from(EnvironmentZone)) == 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "geometry",
    [
        "POLYGON EMPTY",
        "POLYGON((0 0,10 10,0 10,10 0,0 0))",
        "POINT(0 0)",
        "MULTIPOLYGON(((0 0,1 0,1 1,0 0)))",
        "SRID=4326;" + POLYGON,
        "POLYGON Z((0 0 0,1 0 0,1 1 0,0 0 0))",
        "POLYGON M((0 0 0,1 0 0,1 1 0,0 0 0))",
    ],
)
async def test_rejects_invalid_geometry(db, geometry):
    connection, map_id = db
    with pytest.raises(DBAPIError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    "INSERT INTO environment_zones(id, type, geometry, map_id) "
                    "VALUES (:id, 'NO_GO', ST_GeomFromEWKT(:geometry), :map_id)"
                ),
                {"id": uuid4(), "map_id": map_id, "geometry": geometry},
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "missing_map",
        "invalid_type",
        "id",
        "type",
        "geometry",
        "map_id",
        "created_at",
        "updated_at",
    ],
)
async def test_rejects_invalid_zone_rows(db, failure):
    connection, map_id = db
    values = {
        "id": uuid4(),
        "type": "NO_GO",
        "geometry": POLYGON,
        "map_id": map_id,
    }
    expressions = {
        "id": ":id",
        "type": ":type",
        "geometry": "ST_GeomFromText(:geometry, 0)",
        "map_id": ":map_id",
        "created_at": "now()",
        "updated_at": "now()",
    }
    if failure == "missing_map":
        values["map_id"] = uuid4()
    elif failure == "invalid_type":
        values["type"] = "UNKNOWN"
    else:
        expressions[failure] = "NULL"

    with pytest.raises(DBAPIError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    f"INSERT INTO environment_zones ({', '.join(expressions)}) "
                    f"VALUES ({', '.join(expressions.values())})"
                ),
                values,
            )


@pytest.mark.asyncio
async def test_service_appends_touching_zones_and_rejects_existing_overlap(db):
    connection, map_id = db
    group_id = await connection.scalar(select(Map.group_id).where(Map.id == map_id))
    credential = actor(group_id)
    service = service_for_connection(connection)
    await connection.execute(
        MapBoundary.__table__.insert().values(
            map_id=map_id,
            geometry=WKTElement(POLYGON, srid=0),
        )
    )

    await create_zones(
        service,
        credential,
        map_id,
        [
            (EnvironmentZoneType.NO_GO, rectangle(0, 0, 2, 2)),
            (EnvironmentZoneType.CLEANING_ZONE, rectangle(2, 0, 4, 2)),
        ],
    )
    assert (
        await connection.scalar(select(func.count()).select_from(EnvironmentZone)) == 2
    )

    with pytest.raises(BadRequestException, match="existing zone"):
        await create_zones(
            service,
            credential,
            map_id,
            [(EnvironmentZoneType.OBSTACLE, rectangle(1, 1, 3, 3))],
        )
    assert (
        await connection.scalar(select(func.count()).select_from(EnvironmentZone)) == 2
    )


@pytest.mark.asyncio
async def test_same_geometry_is_allowed_on_different_maps(db):
    connection, first_map_id = db
    group_id = await connection.scalar(
        select(Map.group_id).where(Map.id == first_map_id)
    )
    second_map_id = uuid4()
    await connection.execute(
        Map.__table__.insert().values(
            id=second_map_id,
            group_id=group_id,
            name="Second zone map",
            dimension_x=10,
            dimension_y=10,
        )
    )
    for map_id in (first_map_id, second_map_id):
        await connection.execute(
            MapBoundary.__table__.insert().values(
                map_id=map_id,
                geometry=WKTElement(POLYGON, srid=0),
            )
        )

    credential = actor(group_id)
    service = service_for_connection(connection)
    for map_id in (first_map_id, second_map_id):
        await create_zones(
            service,
            credential,
            map_id,
            [(EnvironmentZoneType.OBSTACLE, rectangle(1, 1, 3, 3))],
        )
    assert (
        await connection.scalar(select(func.count()).select_from(EnvironmentZone)) == 2
    )


@pytest.mark.asyncio
async def test_service_rejects_batch_overlap_atomically(db):
    connection, map_id = db
    group_id = await connection.scalar(select(Map.group_id).where(Map.id == map_id))
    credential = actor(group_id)
    service = service_for_connection(connection)
    await connection.execute(
        MapBoundary.__table__.insert().values(
            map_id=map_id,
            geometry=WKTElement(POLYGON, srid=0),
        )
    )

    with pytest.raises(BadRequestException, match="indexes 0 and 1"):
        await create_zones(
            service,
            credential,
            map_id,
            [
                (EnvironmentZoneType.NO_GO, rectangle(0, 0, 4, 4)),
                (EnvironmentZoneType.CLEANING_ZONE, rectangle(1, 1, 2, 2)),
            ],
        )
    assert (
        await connection.scalar(select(func.count()).select_from(EnvironmentZone)) == 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("boundary", "zone", "message"),
    [
        (POLYGON, rectangle(9, 9, 11, 11), "within"),
        (
            "POLYGON((0 0,10 0,10 10,0 10,0 0),(4 4,4 6,6 6,6 4,4 4))",
            rectangle(4.5, 4.5, 5.5, 5.5),
            "within",
        ),
        (POLYGON, {"type": "Polygon", "coordinates": [[[0, 0]] * 4]}, "valid"),
    ],
)
async def test_service_rejects_outside_hole_and_invalid_zones(
    db, boundary, zone, message
):
    connection, map_id = db
    group_id = await connection.scalar(select(Map.group_id).where(Map.id == map_id))
    credential = actor(group_id)
    service = service_for_connection(connection)
    await connection.execute(
        MapBoundary.__table__.insert().values(
            map_id=map_id,
            geometry=WKTElement(boundary, srid=0),
        )
    )

    with pytest.raises(BadRequestException, match=message):
        await create_zones(
            service,
            credential,
            map_id,
            [
                (EnvironmentZoneType.NO_GO, rectangle(0, 0, 1, 1)),
                (EnvironmentZoneType.OBSTACLE, zone),
            ],
        )
    assert (
        await connection.scalar(select(func.count()).select_from(EnvironmentZone)) == 0
    )


@pytest.mark.asyncio
async def test_service_requires_a_boundary(db):
    connection, map_id = db
    group_id = await connection.scalar(select(Map.group_id).where(Map.id == map_id))
    with pytest.raises(BadRequestException, match="boundary is required"):
        await create_zones(
            service_for_connection(connection),
            actor(group_id),
            map_id,
            [(EnvironmentZoneType.NO_GO, rectangle(0, 0, 1, 1))],
        )
    assert (
        await connection.scalar(select(func.count()).select_from(EnvironmentZone)) == 0
    )


@pytest.mark.asyncio
async def test_concurrent_overlapping_batches_are_first_writer_wins():
    url = os.environ.get("MAP_BOUNDARY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires a disposable PostGIS database")

    schema = "environment_zone_concurrency_" + uuid4().hex
    admin_engine = create_async_engine(url)
    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )
    tasks = []
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(
                text("CREATE EXTENSION IF NOT EXISTS postgis WITH SCHEMA public")
            )
            await connection.execute(
                text("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
            )
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        user_id, group_id, map_id = uuid4(), uuid4(), uuid4()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(
                User.__table__.insert().values(
                    id=user_id,
                    name="Zone concurrency",
                    email="zone-concurrency@example.com",
                )
            )
            await connection.execute(
                Group.__table__.insert().values(
                    id=group_id, owner_id=user_id, name="Zone concurrency"
                )
            )
            await connection.execute(
                Map.__table__.insert().values(
                    id=map_id,
                    group_id=group_id,
                    name="Zone concurrency",
                    dimension_x=10,
                    dimension_y=10,
                )
            )
            await connection.execute(
                MapBoundary.__table__.insert().values(
                    map_id=map_id,
                    geometry=WKTElement(POLYGON, srid=0),
                )
            )

        locked = asyncio.Event()
        release = asyncio.Event()
        second_started = asyncio.Event()

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

        first_registry = Registry(engine, None)
        second_registry = Registry(engine, None)
        first_registry._map_repo = FirstMapRepository()
        second_registry._map_repo = SecondMapRepository()
        credential = actor(group_id)
        geometry = [(EnvironmentZoneType.NO_GO, rectangle(1, 1, 3, 3))]

        tasks.append(
            asyncio.create_task(
                create_zones(
                    MapService(first_registry, permissions()),
                    credential,
                    map_id,
                    geometry,
                )
            )
        )
        await asyncio.wait_for(locked.wait(), 10)
        tasks.append(
            asyncio.create_task(
                create_zones(
                    MapService(second_registry, permissions()),
                    credential,
                    map_id,
                    geometry,
                )
            )
        )
        await asyncio.wait_for(second_started.wait(), 10)
        assert not tasks[1].done()
        release.set()

        first, second = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), 15
        )
        assert first is None
        assert isinstance(second, BadRequestException)
        assert "existing zone" in second.message
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    select(func.count()).select_from(EnvironmentZone)
                )
                == 1
            )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()
