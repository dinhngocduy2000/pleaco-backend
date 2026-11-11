"""Full HTTP, PostGIS layout, and confirmed-position preview on an isolated schema."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from geoalchemy2 import WKTElement
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enum.user_roles import GroupRole
from app.common.enum.user_status import UserStatus
from app.common.middleware.auth_middleware import AuthMiddleware
from app.common.schemas.user import Credential
from app.core.rbac.permissions import PermissionService
from app.handler.cleaning_path import CleaningPathHandler
from app.models import DockingStation, Map, MapBoundary, Robot
from app.repository.cleaning_path import CleaningPathRepository
from app.router.cleaning_path import CleaningPathRouter
from app.services.cleaning_path import CleaningPathService
from tests.integration.test_map_boundaries import db  # noqa: F401


@pytest.mark.asyncio
async def test_http_preview_reads_postgis_and_does_not_change_robot_state(db):
    # 1. Seed one map-assigned robot, rectangular boundary, and assigned dock
    # in the test fixture's disposable PostGIS schema.
    connection, map_id = db
    group_id = await connection.scalar(select(Map.group_id).where(Map.id == map_id))
    robot_id = uuid4()
    await connection.execute(Robot.__table__.insert().values(
        id=robot_id, group_id=group_id, map_id=map_id, name="Preview robot",
        serial_num="PREVIEW-1", model="STANDARD", connection_status="ONLINE",
        operational_status="IDLE",
    ))
    await connection.execute(MapBoundary.__table__.insert().values(
        map_id=map_id, geometry=WKTElement("POLYGON((0 0,10 0,10 10,0 10,0 0))", srid=0),
    ))
    await connection.execute(DockingStation.__table__.insert().values(
        map_id=map_id, robot_id=robot_id, heading="NORTH",
        geometry=WKTElement("POLYGON((0.5 0.5,1.5 0.5,1.5 1.5,0.5 1.5,0.5 0.5))", srid=0),
    ))
    # 2. Supply controlled confirmed-position and heartbeat reads without
    # connecting to live Redis or publishing any robot command.
    position = {"group_id": str(group_id), "map_id": str(map_id), "x": 1, "y": 1,
                "updated_at": datetime.now(timezone.utc).isoformat()}
    cached_position = {"value": position}
    store = SimpleNamespace(position=lambda rid: None)

    async def get_position(rid):
        return cached_position["value"] if rid == robot_id else None

    store.position = get_position
    async def get_heartbeat(key):
        return datetime.now(timezone.utc).isoformat()
    store.redis_client = SimpleNamespace(get=get_heartbeat)

    # 3. Bind repository queries to the isolated schema and construct the
    # actual HTTP router, handler, service, and repository chain.
    async def transaction(callback):
        async with AsyncSession(bind=connection, join_transaction_mode="create_savepoint") as session:
            async with session.begin():
                return await callback(session)

    registry = SimpleNamespace(transaction_wrapper=transaction)

    async def member(**kwargs):
        return SimpleNamespace(role=GroupRole.MEMBER)

    permission = SimpleNamespace(get_group_member=member,
                                 is_action_executable=PermissionService.is_action_executable)
    service = CleaningPathService(registry, permission, store, CleaningPathRepository())
    credential = Credential(id=uuid4(), email="preview@example.com",
                            status=UserStatus.ACTIVE, active_group_id=group_id)
    app = FastAPI()
    app.include_router(CleaningPathRouter(CleaningPathHandler(service)).router,
                       prefix="/api/v1/maps")
    app.dependency_overrides[AuthMiddleware.auth_middleware] = lambda: credential
    url = f"/api/v1/maps/{map_id}/cleaning-path/preview"
    before = (await connection.execute(select(Robot.map_id, Robot.operational_status)
                                       .where(Robot.id == robot_id))).one()
    # 4. Check ordered ring output and its dock-to-center-to-dock continuity.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        response = await client.post(url, json={"robot_id": str(robot_id)})
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert set(data) == {"ring_layers", "to_center", "return_to_dock"}
        assert data["ring_layers"][0]["name"] == "ring_layer_1"
        assert data["ring_layers"][0]["points"][0] == {"x": 1, "y": 1}
        sections = [layer["points"] for layer in data["ring_layers"]]
        sections.extend([data["to_center"], data["return_to_dock"]])
        points = [point for index, group in enumerate(sections)
                  for point in (group if index == 0 else group[1:])]
        assert points[0] == points[-1] == {"x": 1, "y": 1}
        assert {"x": 5, "y": 5} in points
        # 5. A missing Redis record falls back to the assigned dock centroid.
        cached_position["value"] = None
        fallback = await client.post(url, json={"robot_id": str(robot_id)})
        assert fallback.status_code == 200, fallback.text
        assert fallback.json()["data"]["ring_layers"][0]["points"][0] == {"x": 1, "y": 1}
        # 6. Reject unknown, stale, and offline robots through the same route.
        cached_position["value"] = position
        assert (await client.post(url, json={"robot_id": str(uuid4())})).status_code == 404
        position["updated_at"] = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
        assert (await client.post(url, json={"robot_id": str(robot_id)})).status_code == 400
        position["updated_at"] = datetime.now(timezone.utc).isoformat()
        await connection.execute(Robot.__table__.update().where(Robot.id == robot_id)
                                 .values(connection_status="OFFLINE"))
        offline = await client.post(url, json={"robot_id": str(robot_id)})
        assert offline.status_code == 400
        assert "online" in offline.text
    # 7. Previewing must leave the persisted robot assignment and state intact.
    after = (await connection.execute(select(Robot.map_id, Robot.operational_status)
                                      .where(Robot.id == robot_id))).one()
    assert after == before
