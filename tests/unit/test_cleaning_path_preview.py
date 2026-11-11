"""Validation before any cleaning path is planned."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from shapely.geometry import Point
from app.common.exceptions import BadRequestException, NotFoundException
from app.common.exceptions import ForbiddenException
from app.common.context import AppContext
from app.common.enum.user_roles import GroupRole
from app.core.rbac.permissions import PermissionService
from app.services.cleaning_path import CleaningPathService
from app.common.schemas.cleaning_path import CleaningPathPreviewData, CleaningPathPreviewRequest
from app.handler.cleaning_path import CleaningPathHandler
from app.router.cleaning_path import CleaningPathRouter


def test_preview_robot_id_is_required_in_body():
    robot_id = uuid4()
    assert CleaningPathPreviewRequest.model_validate({"robot_id": str(robot_id)}).robot_id == robot_id
    with pytest.raises(ValidationError):
        CleaningPathPreviewRequest.model_validate({})


def test_preview_response_has_fixed_fields_and_numbered_ring_names():
    valid = {"ring_layers": [{"name": "ring_layer_1", "points": [{"x": 1, "y": 1}]}],
             "to_center": [{"x": 5, "y": 5}], "return_to_dock": [{"x": 1, "y": 1}]}
    assert CleaningPathPreviewData.model_validate(valid).ring_layers[0].name == "ring_layer_1"
    with pytest.raises(ValidationError):
        CleaningPathPreviewData.model_validate({**valid, "ring_layer_2": []})
    with pytest.raises(ValidationError):
        CleaningPathPreviewData.model_validate({**valid, "ring_layers": [{"name": "other", "points": []}]})


def test_preview_route_declares_request_body():
    app = FastAPI()
    app.include_router(CleaningPathRouter(CleaningPathHandler(Mock())).router,
                       prefix="/api/v1/maps")
    operation = app.openapi()["paths"]["/api/v1/maps/{map_id}/cleaning-path/preview"]["post"]
    assert "requestBody" in operation
    assert operation["responses"]["200"]


def service_with(snapshot, positions):
    registry = SimpleNamespace(transaction_wrapper=AsyncMock(return_value=snapshot))
    store = SimpleNamespace(
        position=AsyncMock(side_effect=lambda robot_id: positions.get(robot_id)),
        redis_client=SimpleNamespace(get=AsyncMock(return_value=datetime.now(timezone.utc).isoformat())),
    )
    return CleaningPathService(registry, Mock(), store, Mock())


def layout(map_id, group_id, robot_ids):
    return {
        "width": 12, "height": 12, "boundary": "DIMENSIONS",
        "robots": [SimpleNamespace(id=rid, map_id=map_id, group_id=group_id,
                                   connection_status="ONLINE") for rid in robot_ids],
        "zones": [],
        "docks": [(robot_ids[0], '{"type":"Polygon","coordinates":[[[0,0],[2,0],[2,2],[0,2],[0,0]]]}')],
    }


@pytest.mark.asyncio
async def test_robot_on_another_map_is_not_found_even_within_group():
    group_id, map_id, robot_id = uuid4(), uuid4(), uuid4()
    snapshot = layout(map_id, group_id, [robot_id])
    snapshot["robots"][0].map_id = uuid4()
    service = service_with(snapshot, {})
    with pytest.raises(NotFoundException):
        await service.preview.__wrapped__(service, map_id=map_id, robot_id=robot_id,
                                          group_id=group_id,
                                          credential=SimpleNamespace(active_group_id=group_id), ctx=None)
    service.positions.position.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_other_robot_position_rejects_before_planning():
    group_id, map_id, robot_id, other_id = uuid4(), uuid4(), uuid4(), uuid4()
    snapshot = layout(map_id, group_id, [robot_id, other_id])
    now = datetime.now(timezone.utc)
    def position(rid, at, x, y):
        return {"group_id": str(group_id), "map_id": str(map_id),
                "updated_at": at.isoformat(), "x": x, "y": y}
    service = service_with(snapshot, {
        robot_id: position(robot_id, now, 1, 1),
        other_id: position(other_id, now - timedelta(seconds=61), 9, 9),
    })
    with patch("app.services.cleaning_path.plan_cleaning_path") as planner:
        with pytest.raises(BadRequestException, match="stale"):
            await service.preview.__wrapped__(service, map_id=map_id, robot_id=robot_id,
                                              group_id=group_id,
                                              credential=SimpleNamespace(active_group_id=group_id), ctx=None)
        planner.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing_dock", "unsupported_boundary"])
async def test_invalid_layout_or_position_rejected(change):
    group_id, map_id, robot_id = uuid4(), uuid4(), uuid4()
    snapshot = layout(map_id, group_id, [robot_id])
    now = datetime.now(timezone.utc)
    positions = {robot_id: {"group_id": str(group_id), "map_id": str(map_id),
                            "updated_at": now.isoformat(), "x": 1, "y": 1}}
    if change == "missing_dock":
        snapshot["docks"] = []
    elif change == "unsupported_boundary":
        snapshot["boundary"] = "CUSTOM"
    service = service_with(snapshot, positions)
    with pytest.raises(BadRequestException):
        await service.preview.__wrapped__(service, map_id=map_id, robot_id=robot_id,
                                          group_id=group_id,
                                          credential=SimpleNamespace(active_group_id=group_id), ctx=None)


@pytest.mark.asyncio
async def test_missing_requested_position_uses_assigned_dock_centroid():
    group_id, map_id, robot_id = uuid4(), uuid4(), uuid4()
    service = service_with(layout(map_id, group_id, [robot_id]), {})
    with patch("app.services.cleaning_path.plan_cleaning_path", return_value={"ring_layer_1": [(1, 1)]}) as planner:
        route = await service.preview.__wrapped__(
            service, map_id=map_id, robot_id=robot_id, group_id=group_id,
            credential=SimpleNamespace(active_group_id=group_id), ctx=None,
        )
    assert route == {"ring_layer_1": [(1, 1)]}
    assert planner.call_args.args[2] == (1.0, 1.0)


@pytest.mark.asyncio
async def test_missing_other_robot_position_uses_its_map_dock():
    group_id, map_id, robot_id, other_id = uuid4(), uuid4(), uuid4(), uuid4()
    snapshot = layout(map_id, group_id, [robot_id, other_id])
    snapshot["docks"].append((other_id, '{"type":"Polygon","coordinates":[[[8,8],[10,8],[10,10],[8,10],[8,8]]]}'))
    service = service_with(snapshot, {robot_id: {
        "group_id": str(group_id), "map_id": str(map_id),
        "updated_at": datetime.now(timezone.utc).isoformat(), "x": 1, "y": 1,
    }})
    with patch("app.services.cleaning_path.plan_cleaning_path", return_value={"ring_layer_1": [(1, 1)]}) as planner:
        await service.preview.__wrapped__(
            service, map_id=map_id, robot_id=robot_id, group_id=group_id,
            credential=SimpleNamespace(active_group_id=group_id), ctx=None,
        )
    assert Point(9, 9) in planner.call_args.args[3]


@pytest.mark.asyncio
async def test_missing_other_robot_position_without_dock_still_rejected():
    group_id, map_id, robot_id, other_id = uuid4(), uuid4(), uuid4(), uuid4()
    snapshot = layout(map_id, group_id, [robot_id, other_id])
    service = service_with(snapshot, {robot_id: {
        "group_id": str(group_id), "map_id": str(map_id),
        "updated_at": datetime.now(timezone.utc).isoformat(), "x": 1, "y": 1,
    }})
    with pytest.raises(BadRequestException, match="assigned docking station"):
        await service.preview.__wrapped__(
            service, map_id=map_id, robot_id=robot_id, group_id=group_id,
            credential=SimpleNamespace(active_group_id=group_id), ctx=None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status,heartbeat", [
    ("OFFLINE", None), ("STALE", None),
    ("ONLINE", None),
    ("ONLINE", (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()),
])
async def test_robot_must_be_online_with_fresh_heartbeat(status, heartbeat):
    group_id, map_id, robot_id = uuid4(), uuid4(), uuid4()
    snapshot = layout(map_id, group_id, [robot_id])
    snapshot["robots"][0].connection_status = status
    service = service_with(snapshot, {})
    service.positions.redis_client.get.return_value = heartbeat
    with pytest.raises(BadRequestException):
        await service.preview.__wrapped__(service, map_id=map_id, robot_id=robot_id,
                                          group_id=group_id,
                                          credential=SimpleNamespace(active_group_id=group_id), ctx=None)
    service.positions.position.assert_not_awaited()


@pytest.mark.asyncio
async def test_context_flows_to_layout_planner_and_logs(caplog):
    group_id, map_id, robot_id = uuid4(), uuid4(), uuid4()
    snapshot = layout(map_id, group_id, [robot_id])
    now = datetime.now(timezone.utc)
    service = service_with(snapshot, {robot_id: {
        "group_id": str(group_id), "map_id": str(map_id),
        "updated_at": now.isoformat(), "x": 1, "y": 1,
    }})
    service.repository.layout = AsyncMock(return_value=snapshot)

    async def transaction(callback):
        return await callback(SimpleNamespace())

    service.registry.transaction_wrapper.side_effect = transaction
    ctx = AppContext(trace_id=uuid4(), action="PREVIEW_CLEANING_PATH", actor=uuid4())
    with patch("app.services.cleaning_path.plan_cleaning_path", return_value={"ring_layer_1": [(1, 1)]}) as planner:
        result = await service.preview.__wrapped__(
            service, map_id=map_id, robot_id=robot_id, group_id=group_id,
            credential=SimpleNamespace(active_group_id=group_id), ctx=ctx,
        )
    assert result == {"ring_layer_1": [(1, 1)]}
    assert service.repository.layout.await_args.args[-1] is ctx
    assert planner.call_args.kwargs["ctx"] is ctx
    assert any(str(ctx.trace_id) in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_member_permission_and_active_group_are_enforced():
    group_id, map_id, robot_id = uuid4(), uuid4(), uuid4()
    service = service_with(layout(map_id, group_id, [robot_id]), {})
    service.permission_service = SimpleNamespace(
        get_group_member=AsyncMock(return_value=SimpleNamespace(role=GroupRole.GUEST)),
        is_action_executable=PermissionService.is_action_executable,
    )
    credential = SimpleNamespace(id=uuid4(), active_group_id=group_id)
    ctx = AppContext(trace_id=uuid4(), action="PREVIEW_CLEANING_PATH", actor=credential.id)
    with pytest.raises(ForbiddenException):
        await service.preview(map_id=map_id, robot_id=robot_id, group_id=group_id,
                              credential=credential, ctx=ctx)
    service.registry.transaction_wrapper.assert_not_awaited()
    service.permission_service.get_group_member.return_value = SimpleNamespace(role=GroupRole.MEMBER)
    with pytest.raises(ForbiddenException):
        await service.preview(map_id=map_id, robot_id=robot_id, group_id=uuid4(),
                              credential=credential, ctx=ctx)
