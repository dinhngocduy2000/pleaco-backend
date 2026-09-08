"""PostGIS integration tests for environment-zone persistence.

Set MAP_BOUNDARY_TEST_DATABASE_URL to a disposable PostgreSQL/PostGIS database.
Application configuration must be supplied without loading protected env files.
"""

import importlib.util
import os
from pathlib import Path
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
from app.core.database import Base
from app.models import EnvironmentZone, Group, Map, User


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
