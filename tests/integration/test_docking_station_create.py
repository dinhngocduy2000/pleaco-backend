"""Service checks on an isolated schema in a disposable PostGIS database.

Supply MAP_BOUNDARY_TEST_DATABASE_URL and disable dotenv loading before imports.
"""

import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from geoalchemy2 import WKTElement
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.common.context import AppContext
from app.common.enum.context_actions import SAVE_DOCKING_STATIONS
from app.common.enum.docking_station import DockingStationHeading
from app.common.enum.environment_zone import EnvironmentZoneType
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import BaseException
from app.common.schemas.map import DockingStationSaveItemDTO, DockingStationsSaveDTO
from app.common.schemas.user import Credential
from app.core.database import Base
from app.core.rbac.permissions import PermissionService
from app.models import (
    DockingStation,
    EnvironmentZone,
    Group,
    Map,
    MapBoundary,
    Robot,
    User,
)
from app.repository.docking_station import (
    DockingStationRepository,
    DockingStationRobotConflict,
)
from app.repository.registry import Registry
from app.services.docking_station import DockingStationService


def polygon(left=0, bottom=0, right=2, top=2):
    return {
        "type": "Polygon",
        "coordinates": [
            [[left, bottom], [right, bottom], [right, top], [left, top], [left, bottom]]
        ],
    }


@pytest_asyncio.fixture
async def station_db():
    url = os.environ.get("MAP_BOUNDARY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires a disposable PostGIS database")
    schema = "station_create_test_" + uuid4().hex
    admin_engine = create_async_engine(url)
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": f"{schema},public"}}
    )
    user_id, group_id, map_id, robot_id = (uuid4() for _ in range(4))
    try:
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.execute(
                text("CREATE EXTENSION IF NOT EXISTS postgis WITH SCHEMA public")
            )
            await conn.execute(
                text("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
            )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(
                User.__table__.insert().values(
                    id=user_id, name="Station", email="station@example.com"
                )
            )
            await conn.execute(
                Group.__table__.insert().values(
                    id=group_id, owner_id=user_id, name="Station"
                )
            )
            await conn.execute(
                Map.__table__.insert().values(
                    id=map_id,
                    group_id=group_id,
                    name="Station",
                    dimension_x=10,
                    dimension_y=10,
                )
            )
            await conn.execute(
                Robot.__table__.insert().values(
                    id=robot_id,
                    group_id=group_id,
                    map_id=map_id,
                    name="Robot",
                    serial_num="STATION",
                    model="STANDARD",
                    connection_status="OFFLINE",
                    operational_status="IDLE",
                )
            )
            await conn.execute(
                MapBoundary.__table__.insert().values(
                    map_id=map_id,
                    geometry=WKTElement("POLYGON((0 0,10 0,10 10,0 10,0 0))", srid=0),
                )
            )
        credential = Credential(
            id=user_id,
            email="station@example.com",
            status=UserStatus.ACTIVE,
            active_group_id=group_id,
        )

        async def member(**kwargs):
            return SimpleNamespace(role=GroupRole.ADMIN)

        permission = SimpleNamespace(
            get_group_member=member,
            is_action_executable=PermissionService.is_action_executable,
        )
        registry = Registry(pg_engine=engine, redis_client=SimpleNamespace())
        service = DockingStationService(registry, permission)
        yield SimpleNamespace(
            engine=engine,
            service=service,
            credential=credential,
            map_id=map_id,
            robot_id=robot_id,
        )
    finally:
        await engine.dispose()
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


async def save(db, items):
    return await db.service.save_docking_stations(
        station_save=DockingStationsSaveDTO(map_id=db.map_id, data=items),
        group_id=db.credential.active_group_id,
        credential=db.credential,
        ctx=AppContext(
            trace_id=uuid4(), actor=db.credential.id, action=SAVE_DOCKING_STATIONS
        ),
    )


async def create(db, **payload):
    return (await save(db, [{"geometry": polygon(), **payload}]))[0]


async def count(db):
    async with db.engine.connect() as conn:
        return await conn.scalar(select(func.count()).select_from(DockingStation))


@pytest.mark.asyncio
async def test_creation_overlaps_nulls_and_serialization(station_db):
    db = station_db
    async with db.engine.begin() as conn:
        await conn.execute(
            EnvironmentZone.__table__.insert().values(
                map_id=db.map_id,
                type=EnvironmentZoneType.NO_GO,
                geometry=WKTElement("POLYGON((0 0,2 0,2 2,0 2,0 0))", srid=0),
            )
        )
    first, second, assigned = await save(
        db,
        [
            {"geometry": polygon(), "heading": None},
            {"geometry": polygon()},
            {"geometry": polygon(), "robot_id": db.robot_id, "heading": "WEST"},
        ],
    )
    assert first.heading == second.heading == DockingStationHeading.SOUTH
    assert first.robot_id is second.robot_id is None
    assert (
        assigned.robot_id == db.robot_id
        and assigned.heading == DockingStationHeading.WEST
    )
    assert first.geometry.model_dump(mode="json") == polygon()
    assert first.created_at.tzinfo is not None and first.updated_at.tzinfo is not None
    assert await count(db) == 3
    async with db.engine.connect() as conn:
        assert set(
            (await conn.scalars(select(func.ST_SRID(DockingStation.geometry)))).all()
        ) == {0}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,code",
    [
        ("missing_map", 404),
        ("foreign_map", 404),
        ("missing_robot", 404),
        ("foreign_robot", 404),
        ("unassigned_robot", 400),
        ("other_map_robot", 400),
        ("no_boundary", 400),
        ("outside", 400),
        ("invalid", 400),
        ("hole", 400),
    ],
)
async def test_invalid_creation_rolls_back(station_db, failure, code):
    db = station_db
    payload = {"robot_id": db.robot_id}
    async with db.engine.begin() as conn:
        other_group = uuid4()
        if failure in ("foreign_map", "foreign_robot"):
            await conn.execute(
                Group.__table__.insert().values(
                    id=other_group, name="Other", owner_id=db.credential.id
                )
            )
        if failure == "missing_map":
            db.map_id = uuid4()
        if failure == "foreign_map":
            await conn.execute(
                Map.__table__.update()
                .where(Map.id == db.map_id)
                .values(group_id=other_group)
            )
        if failure == "missing_robot":
            payload["robot_id"] = uuid4()
        if failure == "foreign_robot":
            await conn.execute(
                Robot.__table__.update()
                .where(Robot.id == db.robot_id)
                .values(group_id=other_group)
            )
        if failure == "unassigned_robot":
            await conn.execute(
                Robot.__table__.update()
                .where(Robot.id == db.robot_id)
                .values(map_id=None)
            )
        if failure == "other_map_robot":
            other_map = uuid4()
            await conn.execute(
                Map.__table__.insert().values(
                    id=other_map,
                    group_id=db.credential.active_group_id,
                    name="Other",
                    dimension_x=10,
                    dimension_y=10,
                )
            )
            await conn.execute(
                Robot.__table__.update()
                .where(Robot.id == db.robot_id)
                .values(map_id=other_map)
            )
        if failure == "no_boundary":
            await conn.execute(MapBoundary.__table__.delete())
        if failure == "outside":
            payload["geometry"] = polygon(9, 9, 11, 11)
        if failure == "invalid":
            payload["geometry"] = {
                "type": "Polygon",
                "coordinates": [[[0, 0], [2, 2], [0, 2], [2, 0], [0, 0]]],
            }
        if failure == "hole":
            await conn.execute(
                MapBoundary.__table__.update().values(
                    geometry=WKTElement(
                        "POLYGON((0 0,10 0,10 10,0 10,0 0),(3 3,3 7,7 7,7 3,3 3))",
                        srid=0,
                    )
                )
            )
            payload["geometry"] = polygon(4, 4, 5, 5)
    with pytest.raises(BaseException) as error:
        await create(db, **payload)
    assert error.value.status_code == code
    assert await count(db) == 0


@pytest.mark.asyncio
async def test_concurrent_stale_ids(station_db):
    db = station_db
    first, second = await save(db, [{"geometry": polygon()}, {"geometry": polygon()}])
    outcomes = await asyncio.wait_for(
        asyncio.gather(
            save(db, [{"id": first.id, "geometry": polygon()}]),
            save(db, [{"id": second.id, "geometry": polygon()}]),
            return_exceptions=True,
        ),
        timeout=15,
    )
    failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(failures) == 1
    assert isinstance(failures[0], BaseException) and failures[0].status_code == 409
    assert await count(db) == 1


@pytest.mark.asyncio
async def test_database_conflict_translation_is_specific(station_db):
    db = station_db
    await create(db, robot_id=db.robot_id)
    repository = DockingStationRepository()
    geometry_json = DockingStationSaveItemDTO(
        geometry=polygon()
    ).geometry.model_dump_json()
    kwargs = dict(
        map_id=db.map_id,
        stations=[
            (None, None, DockingStationHeading.SOUTH, geometry_json),
            (None, db.robot_id, DockingStationHeading.SOUTH, geometry_json),
        ],
        ctx=AppContext(
            trace_id=uuid4(), actor=db.credential.id, action=SAVE_DOCKING_STATIONS
        ),
    )
    async with AsyncSession(db.engine) as session:
        async with session.begin():
            with pytest.raises(DockingStationRobotConflict) as error:
                await repository.save_many(session=session, **kwargs)
            assert error.value.index == 1
            kwargs["stations"] = [
                (None, uuid4(), DockingStationHeading.SOUTH, geometry_json)
            ]
            with pytest.raises(IntegrityError):
                await repository.save_many(session=session, **kwargs)
    assert await count(db) == 1


def item(station, **changes):
    return {
        "id": station.id,
        "geometry": station.geometry.model_dump(mode="json"),
        "heading": station.heading,
        "robot_id": station.robot_id,
        **changes,
    }


@pytest.mark.asyncio
async def test_synchronize_update_delete_clear_and_defaults(station_db):
    db = station_db
    first, omitted = await save(
        db,
        [
            {"geometry": polygon(), "robot_id": db.robot_id, "heading": "WEST"},
            {"geometry": polygon()},
        ],
    )
    result = await save(
        db, [{"geometry": polygon(3, 3, 4, 4)}, {"id": first.id, "geometry": polygon()}]
    )
    assert result[1].id == first.id and result[1].created_at == first.created_at
    assert result[1].updated_at >= first.updated_at
    assert (
        result[1].heading == DockingStationHeading.SOUTH and result[1].robot_id is None
    )
    assert result[0].id not in (first.id, omitted.id)
    assert result[0].geometry.model_dump(mode="json") == polygon(3, 3, 4, 4)
    assert await count(db) == 2
    async with db.engine.begin() as conn:
        await conn.execute(MapBoundary.__table__.delete())
    assert await save(db, []) == []
    assert await count(db) == 0


@pytest.mark.asyncio
async def test_robot_swaps_and_transfer_from_deleted_station(station_db):
    db = station_db
    other = uuid4()
    async with db.engine.begin() as conn:
        await conn.execute(
            Robot.__table__.insert().values(
                id=other,
                group_id=db.credential.active_group_id,
                map_id=db.map_id,
                name="Other",
                serial_num="OTHER",
                model="STANDARD",
                connection_status="OFFLINE",
                operational_status="IDLE",
            )
        )
    first, second = await save(
        db,
        [
            {"geometry": polygon(), "robot_id": db.robot_id},
            {"geometry": polygon(), "robot_id": other},
        ],
    )
    swapped = await save(
        db, [item(first, robot_id=other), item(second, robot_id=db.robot_id)]
    )
    assert [row.robot_id for row in swapped] == [other, db.robot_id]
    assert [row.id for row in swapped] == [first.id, second.id]
    moved = await save(db, [item(swapped[0], robot_id=db.robot_id)])
    assert moved[0].id == first.id and moved[0].robot_id == db.robot_id
    replacement = await create(db, robot_id=db.robot_id)
    assert replacement.id != first.id and await count(db) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["stale", "geometry", "foreign_id", "foreign_assignment"]
)
async def test_failed_batch_preserves_existing_rows(station_db, failure):
    db = station_db
    original = await create(db, robot_id=db.robot_id)
    bad = {"geometry": polygon()}
    expected = 409
    async with db.engine.begin() as conn:
        if failure in ("foreign_id", "foreign_assignment"):
            other_map = uuid4()
            await conn.execute(
                Map.__table__.insert().values(
                    id=other_map,
                    group_id=db.credential.active_group_id,
                    name="Other",
                    dimension_x=10,
                    dimension_y=10,
                )
            )
            foreign_id = uuid4()
            await conn.execute(
                DockingStation.__table__.insert().values(
                    id=foreign_id,
                    map_id=other_map,
                    heading="SOUTH",
                    geometry=WKTElement("POLYGON((0 0,2 0,2 2,0 0))", srid=0),
                )
            )
            if failure == "foreign_id":
                bad["id"] = foreign_id
            else:
                await conn.execute(
                    DockingStation.__table__.update()
                    .where(DockingStation.id == original.id)
                    .values(robot_id=None)
                )
                await conn.execute(
                    DockingStation.__table__.update()
                    .where(DockingStation.id == foreign_id)
                    .values(robot_id=db.robot_id)
                )
                bad["robot_id"] = db.robot_id
        if failure == "stale":
            bad["id"] = uuid4()
        if failure == "geometry":
            bad["geometry"] = polygon(9, 9, 11, 11)
            expected = 400
    with pytest.raises(BaseException) as error:
        await save(db, [{"geometry": polygon()}, bad])
    assert error.value.status_code == expected and "index 1" in error.value.detail
    async with db.engine.connect() as conn:
        assert (
            await conn.scalar(
                select(DockingStation.id).where(DockingStation.map_id == db.map_id)
            )
            == original.id
        )


@pytest.mark.asyncio
async def test_write_failure_rolls_back_deletions_and_updates(station_db, monkeypatch):
    db = station_db
    first, second = await save(
        db, [{"geometry": polygon(), "robot_id": db.robot_id}, {"geometry": polygon()}]
    )
    repository = db.service.repo.docking_station_repo()
    save_many = repository.save_many

    async def fail(**kwargs):
        if kwargs["stations"][0][0] is None:
            raise DockingStationRobotConflict(index=0)
        return await save_many(**kwargs)

    monkeypatch.setattr(repository, "save_many", fail)
    with pytest.raises(BaseException) as error:
        await save(
            db, [item(first, robot_id=None, heading="EAST"), {"geometry": polygon()}]
        )
    assert error.value.status_code == 409 and "index 1" in error.value.detail
    async with db.engine.connect() as conn:
        rows = (
            await conn.execute(
                select(
                    DockingStation.id, DockingStation.robot_id, DockingStation.heading
                )
            )
        ).all()
    assert len(rows) == 2
    restored = next(row for row in rows if row.id == first.id)
    assert restored.robot_id == db.robot_id and restored.heading == first.heading


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [1, 100])
async def test_batch_query_count(station_db, size):
    from sqlalchemy import event

    db = station_db
    robot_ids = [uuid4() for _ in range(size)]
    async with db.engine.begin() as conn:
        await conn.execute(
            Robot.__table__.insert(),
            [
                dict(
                    id=robot_id,
                    group_id=db.credential.active_group_id,
                    map_id=db.map_id,
                    name=f"Batch {index}",
                    serial_num=f"BATCH-{index}",
                    model="STANDARD",
                    connection_status="OFFLINE",
                    operational_status="IDLE",
                )
                for index, robot_id in enumerate(robot_ids)
            ],
        )
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.engine.sync_engine, "before_cursor_execute", capture)
    try:
        result = await save(
            db,
            [{"geometry": polygon(), "robot_id": robot_id} for robot_id in robot_ids],
        )
        print(f"insert size={size} statements={len(statements)}")
        assert len(statements) == 8
        statements.clear()
        await save(db, [item(row, heading="WEST") for row in result])
        print(f"update size={size} statements={len(statements)}")
        assert len(statements) == 8
    finally:
        event.remove(db.engine.sync_engine, "before_cursor_execute", capture)
