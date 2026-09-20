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
from app.common.enum.context_actions import CREATE_DOCKING_STATION
from app.common.enum.docking_station import DockingStationHeading
from app.common.enum.environment_zone import EnvironmentZoneType
from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.exceptions import BaseException
from app.common.schemas.map import DockingStationCreateDTO
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


async def create(db, **payload):
    return await db.service.create_docking_station(
        map_id=db.map_id,
        station_create=DockingStationCreateDTO(**{"geometry": polygon(), **payload}),
        group_id=db.credential.active_group_id,
        credential=db.credential,
        ctx=AppContext(
            trace_id=uuid4(), actor=db.credential.id, action=CREATE_DOCKING_STATION
        ),
    )


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
    first = await create(db, heading=None)
    second = await create(db)
    assigned = await create(db, robot_id=db.robot_id, heading="WEST")
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
async def test_concurrent_robot_assignment(station_db):
    db = station_db
    outcomes = await asyncio.wait_for(
        asyncio.gather(
            create(db, robot_id=db.robot_id),
            create(db, robot_id=db.robot_id),
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
    kwargs = dict(
        map_id=db.map_id,
        robot_id=db.robot_id,
        heading=DockingStationHeading.SOUTH,
        geometry_json=DockingStationCreateDTO(
            geometry=polygon()
        ).geometry.model_dump_json(),
        ctx=AppContext(
            trace_id=uuid4(), actor=db.credential.id, action=CREATE_DOCKING_STATION
        ),
    )
    async with AsyncSession(db.engine) as session:
        async with session.begin():
            with pytest.raises(DockingStationRobotConflict):
                await repository.create(session=session, **kwargs)
            kwargs["robot_id"] = uuid4()
            with pytest.raises(IntegrityError):
                await repository.create(session=session, **kwargs)
    assert await count(db) == 1
