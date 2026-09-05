"""PostGIS integration tests using an isolated, rolled-back schema.

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

from app.common.enum.map import MapBoundarySource
from app.core.database import Base
from app.models import Group, Map, MapBoundary, User


POLYGON = "POLYGON((0 0,10 0,10 10,0 10,0 0))"
MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic/versions/0015_create_map_boundaries.py"
)
spec = importlib.util.spec_from_file_location("boundary_migration", MIGRATION_PATH)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def run_migration(connection, direction):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration, direction)()


@pytest_asyncio.fixture
async def db():
    url = os.environ.get("MAP_BOUNDARY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("MAP_BOUNDARY_TEST_DATABASE_URL requires a disposable PostGIS database")
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                schema = "boundary_test_" + uuid4().hex
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
                await connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
                await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public"))
                await connection.run_sync(
                    lambda sync: Base.metadata.create_all(
                        sync,
                        tables=[table for table in Base.metadata.sorted_tables
                                if table.name != "map_boundaries"],
                    )
                )
                await connection.run_sync(run_migration, "upgrade")
                user_id, group_id, map_id = uuid4(), uuid4(), uuid4()
                await connection.execute(User.__table__.insert().values(
                    id=user_id, name="Boundary test", email="boundary@example.com",
                ))
                await connection.execute(Group.__table__.insert().values(
                    id=group_id, name="Boundary test", owner_id=user_id,
                ))
                await connection.execute(Map.__table__.insert().values(
                    id=map_id, group_id=group_id, name="Boundary test",
                    dimension_x=10, dimension_y=10,
                ))
                yield connection, map_id
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_defaults_round_trip_and_downgrade(db):
    connection, map_id = db
    await connection.execute(text(
        "INSERT INTO map_boundaries(id, map_id, geometry) "
        "VALUES (:id, :map_id, ST_GeomFromText(:geometry, 0))"
    ), {"id": uuid4(), "map_id": map_id, "geometry": POLYGON})
    row = (await connection.execute(text(
        "SELECT source, ST_AsText(geometry), created_at, updated_at FROM map_boundaries"
    ))).one()
    assert row[0] == "DIMENSIONS"
    assert row[1] == POLYGON
    assert row[2].tzinfo is not None and row[3] == row[2]
    indexes = (await connection.execute(text(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
        "AND tablename = 'map_boundaries'"
    ))).scalars().all()
    assert len(indexes) == 2
    assert not any("gist" in index.lower() for index in indexes)
    await connection.run_sync(run_migration, "downgrade")
    assert await connection.scalar(text("SELECT to_regclass('map_boundaries')")) is None
    assert await connection.scalar(text("SELECT to_regtype('map_boundary_source')")) is None
    assert await connection.scalar(text("SELECT count(*) FROM pg_extension WHERE extname='postgis'")) == 1
    assert await connection.scalar(select(func.count()).select_from(Map)) == 1
    await connection.run_sync(run_migration, "upgrade")
    assert await connection.scalar(select(func.count()).select_from(MapBoundary)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("source", list(MapBoundarySource))
async def test_orm_relationship_timestamps_and_orphan_deletion(db, source):
    connection, map_id = db
    async with AsyncSession(bind=connection, expire_on_commit=False) as session:
        map_record = await session.scalar(select(Map).options(selectinload(Map.boundary)))
        assert map_record.boundary is None
        boundary = MapBoundary(geometry=WKTElement(POLYGON, srid=0), source=source)
        map_record.boundary = boundary
        await session.flush()
        assert boundary.id is not None and boundary.map_id == map_id
        assert boundary.map is map_record
        assert boundary.source == source and boundary.geometry is not None
        await session.refresh(boundary)
        assert boundary.geometry.srid == 0
        # A loaded local polygon must remain writable without a fabricated SRID.
        await connection.execute(MapBoundary.__table__.update().values(
            geometry=boundary.geometry,
        ))
        assert await connection.scalar(select(func.ST_SRID(MapBoundary.geometry))) == 0
        await connection.execute(text(
            "UPDATE map_boundaries SET updated_at = '2000-01-01'::timestamptz"
        ))
        await session.refresh(boundary)
        previous = boundary.updated_at
        boundary.geometry = WKTElement("POLYGON((0 0,5 0,5 5,0 5,0 0))", srid=0)
        await session.flush()
        await session.refresh(boundary)
        assert boundary.updated_at > previous
        map_record.boundary = None
        await session.flush()
        assert await session.scalar(select(func.count()).select_from(MapBoundary)) == 0


@pytest.mark.asyncio
async def test_polygon_holes_and_database_cascade(db):
    connection, map_id = db
    await connection.execute(MapBoundary.__table__.insert().values(
        map_id=map_id,
        geometry=WKTElement("POLYGON((0 0,10 0,10 10,0 10,0 0),(2 2,2 4,4 4,4 2,2 2))", srid=0),
    ))
    assert await connection.scalar(select(MapBoundary.source)) == MapBoundarySource.DIMENSIONS
    await connection.execute(Map.__table__.delete().where(Map.id == map_id))
    assert await connection.scalar(select(func.count()).select_from(MapBoundary)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("loaded", [False, True])
async def test_orm_map_deletion_cascades_with_loaded_or_unloaded_boundary(db, loaded):
    connection, map_id = db
    await connection.execute(MapBoundary.__table__.insert().values(
        map_id=map_id, geometry=WKTElement(POLYGON, srid=0),
    ))
    async with AsyncSession(bind=connection) as session:
        query = select(Map).where(Map.id == map_id)
        if loaded:
            query = query.options(selectinload(Map.boundary))
        map_record = await session.scalar(query)
        await session.delete(map_record)
        await session.flush()
        assert await session.scalar(select(func.count()).select_from(MapBoundary)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("geometry", [
    "POLYGON EMPTY",
    "POLYGON((0 0,10 10,0 10,10 0,0 0))",
    "POINT(0 0)",
    "MULTIPOLYGON(((0 0,1 0,1 1,0 0)))",
    "SRID=4326;" + POLYGON,
    "POLYGON Z((0 0 0,1 0 0,1 1 0,0 0 0))",
    "POLYGON M((0 0 0,1 0 0,1 1 0,0 0 0))",
])
async def test_rejects_invalid_geometry(db, geometry):
    connection, map_id = db
    with pytest.raises(DBAPIError):
        async with connection.begin_nested():
            await connection.execute(text(
                "INSERT INTO map_boundaries(id, map_id, geometry) "
                "VALUES (:id, :map_id, ST_GeomFromEWKT(:geometry))"
            ), {"id": uuid4(), "map_id": map_id, "geometry": geometry})


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "duplicate_map", "missing_map", "invalid_source",
    "id", "map_id", "source", "geometry", "created_at", "updated_at",
])
async def test_rejects_invalid_boundary_rows(db, failure):
    connection, map_id = db
    values = dict(id=uuid4(), map_id=map_id, source="CUSTOM", geometry=POLYGON)
    expressions = {
        "id": ":id", "map_id": ":map_id", "source": ":source",
        "geometry": "ST_GeomFromText(:geometry, 0)",
        "created_at": "now()", "updated_at": "now()",
    }
    if failure == "duplicate_map":
        await connection.execute(MapBoundary.__table__.insert().values(
            map_id=map_id, geometry=WKTElement(POLYGON, srid=0),
        ))
    elif failure == "missing_map":
        values["map_id"] = uuid4()
    elif failure == "invalid_source":
        values["source"] = "UNKNOWN"
    else:
        expressions[failure] = "NULL"
    with pytest.raises(DBAPIError):
        async with connection.begin_nested():
            await connection.execute(text(
                f"INSERT INTO map_boundaries ({', '.join(expressions)}) "
                f"VALUES ({', '.join(expressions.values())})"
            ), values)
