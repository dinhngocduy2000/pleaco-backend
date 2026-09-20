"""Persistence checks using MAP_BOUNDARY_TEST_DATABASE_URL (disposable PostGIS).

Supply test settings with environment-file loading disabled before app imports.
"""

import importlib.util
import os
from datetime import datetime, timezone
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

from app.common.enum.docking_station import DockingStationHeading
from app.common.enum.robot import (
    RobotConnectionStatus,
    RobotModel,
    RobotOperationalStatus,
)
from app.core.database import Base
from app.models import DockingStation, Group, Map, Robot, User


POLYGON = "POLYGON((0 0,10 0,10 10,0 10,0 0))"
MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic/versions/0018_create_docking_station.py"
)
spec = importlib.util.spec_from_file_location(
    "docking_station_migration", MIGRATION_PATH
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
                schema = "docking_station_test_" + uuid4().hex
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
                            if table.name != "docking_station"
                        ],
                    )
                )
                await connection.run_sync(run_migration, "upgrade")
                user_id, group_id = uuid4(), uuid4()
                await connection.execute(
                    User.__table__.insert().values(
                        id=user_id, name="Docking test", email="docking@example.com"
                    )
                )
                await connection.execute(
                    Group.__table__.insert().values(
                        id=group_id, name="Docking test", owner_id=user_id
                    )
                )
                map_ids = [uuid4(), uuid4()]
                for index, map_id in enumerate(map_ids):
                    await connection.execute(
                        Map.__table__.insert().values(
                            id=map_id,
                            group_id=group_id,
                            name=f"Docking map {index}",
                            dimension_x=10,
                            dimension_y=10,
                        )
                    )
                robot_ids = [uuid4() for _ in DockingStationHeading]
                for index, robot_id in enumerate(robot_ids):
                    await connection.execute(
                        Robot.__table__.insert().values(
                            id=robot_id,
                            group_id=group_id,
                            map_id=map_ids[0],
                            name=f"Robot {index}",
                            serial_num=f"DOCK-{index}",
                            model=RobotModel.STANDARD,
                            connection_status=RobotConnectionStatus.OFFLINE,
                            operational_status=RobotOperationalStatus.IDLE,
                        )
                    )
                yield connection, map_ids, robot_ids
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


async def insert_station(connection, map_id, robot_id):
    return await connection.scalar(
        DockingStation.__table__.insert()
        .values(
            map_id=map_id,
            robot_id=robot_id,
            heading=DockingStationHeading.NORTH,
            geometry=WKTElement(POLYGON, srid=0),
        )
        .returning(DockingStation.id)
    )


@pytest.mark.asyncio
async def test_relationships_geometry_headings_and_timestamps(db):
    connection, map_ids, robot_ids = db
    async with AsyncSession(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    ) as session:
        map_record = await session.scalar(
            select(Map)
            .where(Map.id == map_ids[0])
            .options(selectinload(Map.docking_stations))
        )
        robots = {
            robot.id: robot
            for robot in (
                await session.scalars(
                    select(Robot).options(selectinload(Robot.docking_station))
                )
            ).all()
        }
        for heading, robot_id in zip(DockingStationHeading, robot_ids):
            assert robots[robot_id].docking_station is None
            map_record.docking_stations.append(
                DockingStation(
                    robot=robots[robot_id],
                    heading=heading,
                    geometry=WKTElement(POLYGON, srid=0),
                )
            )
        await session.flush()
        session.expunge_all()
        map_record = await session.scalar(
            select(Map)
            .where(Map.id == map_ids[0])
            .options(
                selectinload(Map.docking_stations)
                .selectinload(DockingStation.robot)
                .selectinload(Robot.docking_station)
            )
        )
        assert len(map_record.docking_stations) == 4
        assert {station.heading for station in map_record.docking_stations} == set(
            DockingStationHeading
        )
        for station in map_record.docking_stations:
            assert station.map is map_record
            assert station.robot.docking_station is station
            assert station.id is not None
            assert station.geometry.srid == 0
            assert station.created_at.tzinfo is not None
            assert station.updated_at.tzinfo is not None
            assert (
                await session.scalar(
                    select(func.ST_AsText(DockingStation.geometry)).where(
                        DockingStation.id == station.id
                    )
                )
                == POLYGON
            )

        station = map_record.docking_stations[0]
        # Re-bind decoded EWKB to exercise LocalMapGeometry's SRID-0 converter.
        await connection.execute(
            DockingStation.__table__.update()
            .where(DockingStation.id == station.id)
            .values(
                geometry=station.geometry,
                updated_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
            )
        )
        await session.refresh(station)
        previous, created = station.updated_at, station.created_at
        station.heading = (
            DockingStationHeading.SOUTH
            if station.heading != DockingStationHeading.SOUTH
            else DockingStationHeading.NORTH
        )
        await session.flush()
        await session.refresh(station)
        assert station.updated_at > previous
        assert station.created_at == created
        assert (
            await session.scalar(
                select(func.ST_SRID(DockingStation.geometry)).where(
                    DockingStation.id == station.id
                )
            )
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("other_map", [False, True])
async def test_robot_uniqueness_across_maps(db, other_map):
    connection, map_ids, robot_ids = db
    await insert_station(connection, map_ids[0], robot_ids[0])
    with pytest.raises(DBAPIError):
        async with connection.begin_nested():
            await insert_station(connection, map_ids[int(other_map)], robot_ids[0])


@pytest.mark.asyncio
async def test_independent_map_reference(db):
    connection, map_ids, robot_ids = db
    await insert_station(connection, map_ids[1], robot_ids[0])
    assert await connection.scalar(select(DockingStation.map_id)) == map_ids[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", [Map, Robot])
@pytest.mark.parametrize("via_orm", [False, True])
async def test_parent_deletion_cascades(db, parent, via_orm):
    connection, map_ids, robot_ids = db
    await insert_station(connection, map_ids[0], robot_ids[0])
    parent_id = map_ids[0] if parent is Map else robot_ids[0]
    if via_orm:
        relation = Map.docking_stations if parent is Map else Robot.docking_station
        async with AsyncSession(
            bind=connection, join_transaction_mode="create_savepoint"
        ) as session:
            record = await session.scalar(
                select(parent)
                .where(parent.id == parent_id)
                .options(selectinload(relation))
            )
            await session.delete(record)
            await session.flush()
            assert (
                await session.scalar(select(func.count()).select_from(DockingStation))
                == 0
            )
    else:
        await connection.execute(
            parent.__table__.delete().where(parent.id == parent_id)
        )
        assert (
            await connection.scalar(select(func.count()).select_from(DockingStation))
            == 0
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
async def test_invalid_geometry_rejected(db, geometry):
    connection, map_ids, robot_ids = db
    with pytest.raises(DBAPIError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    "INSERT INTO docking_station(id, map_id, robot_id, geometry, heading) "
                    "VALUES (:id, :map_id, :robot_id, ST_GeomFromEWKT(:geometry), 'NORTH')"
                ),
                dict(
                    id=uuid4(),
                    map_id=map_ids[0],
                    robot_id=robot_ids[0],
                    geometry=geometry,
                ),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "missing_map",
        "missing_robot",
        "invalid_heading",
        "id",
        "map_id",
        "robot_id",
        "geometry",
        "heading",
        "created_at",
        "updated_at",
    ],
)
async def test_invalid_rows_rejected(db, failure):
    connection, map_ids, robot_ids = db
    values = dict(
        id=uuid4(),
        map_id=map_ids[0],
        robot_id=robot_ids[0],
        geometry=POLYGON,
        heading="NORTH",
    )
    expressions = dict(
        id=":id",
        map_id=":map_id",
        robot_id=":robot_id",
        geometry="ST_GeomFromText(:geometry, 0)",
        heading=":heading",
        created_at="now()",
        updated_at="now()",
    )
    if failure == "missing_map":
        values["map_id"] = uuid4()
    elif failure == "missing_robot":
        values["robot_id"] = uuid4()
    elif failure == "invalid_heading":
        values["heading"] = "NORTHEAST"
    else:
        expressions[failure] = "NULL"
    with pytest.raises(DBAPIError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    f"INSERT INTO docking_station ({', '.join(expressions)}) "
                    f"VALUES ({', '.join(expressions.values())})"
                ),
                values,
            )


@pytest.mark.asyncio
async def test_migration_contract_and_round_trip(db):
    connection, map_ids, robot_ids = db
    labels = (
        await connection.scalars(
            text(
                "SELECT enumlabel FROM pg_enum JOIN pg_type ON pg_type.oid = pg_enum.enumtypid "
                "WHERE pg_type.typname = 'docking_station_heading' "
                "AND pg_type.typnamespace = current_schema()::regnamespace ORDER BY enumsortorder"
            )
        )
    ).all()
    assert labels == [heading.value for heading in DockingStationHeading]
    indexes = (
        await connection.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = current_schema() AND tablename = 'docking_station'"
            )
        )
    ).all()
    assert {row.indexname for row in indexes} == {
        "docking_station_pkey",
        "uq_docking_station_robot_id",
        "ix_docking_station_map_id",
    }
    assert not any("gist" in row.indexdef.lower() for row in indexes)
    await insert_station(connection, map_ids[0], robot_ids[0])
    await connection.run_sync(run_migration, "downgrade")
    assert (
        await connection.scalar(text("SELECT to_regclass('docking_station')")) is None
    )
    assert (
        await connection.scalar(text("SELECT to_regtype('docking_station_heading')"))
        is None
    )
    assert await connection.scalar(select(func.count()).select_from(Map)) == 2
    assert await connection.scalar(select(func.count()).select_from(Robot)) == 4
    assert (
        await connection.scalar(text("SELECT to_regclass('environment_zones')"))
        is not None
    )
    assert (
        await connection.scalar(
            text("SELECT count(*) FROM pg_extension WHERE extname='postgis'")
        )
        == 1
    )
    await connection.run_sync(run_migration, "upgrade")
    await insert_station(connection, map_ids[0], robot_ids[0])
    assert (
        await connection.scalar(select(func.count()).select_from(DockingStation)) == 1
    )
