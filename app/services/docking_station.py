from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.context import AppContext
from app.common.enum.docking_station import DockingStationHeading
from app.common.enum.user_roles import GroupRole
from app.common.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
)
from app.common.middleware.logger import Logger
from app.common.schemas.geometry import PolygonGeometry
from app.common.schemas.map import (
    DockingStationInfo,
    DockingStationSaveItemDTO,
    DockingStationsSaveDTO,
)
from app.common.schemas.user import Credential
from app.core.rbac.permissions import PermissionService
from app.core.rbac.role_validation import require_permission
from app.models.robot import Robot
from app.repository.docking_station import (
    BoundaryPlacementInspection,
    DockingStationRobotConflict,
)
from app.repository.registry import Registry

logger = Logger()
IndexedStation = tuple[int, DockingStationSaveItemDTO]


class DockingStationService:
    def __init__(self, repo: Registry, permission_service: PermissionService) -> None:
        self.repo = repo
        self.permission_service = permission_service

    @require_permission(GroupRole.ADMIN)
    async def assign_robot(
        self,
        map_id: UUID,
        docking_id: UUID,
        robot_id: UUID,
        group_id: UUID | None,
        credential: Credential,
        ctx: AppContext,
    ) -> None:
        self._validate_active_group(group_id, credential)

        async def assign(session: AsyncSession) -> None:
            # Match layout saves: lock the map before its robots.
            await self._lock_map(session, map_id, group_id, ctx)
            repository = self.repo.docking_station_repo()
            station = await repository.get_for_map(
                session=session, map_id=map_id, docking_id=docking_id, ctx=ctx
            )
            if station is None:
                raise NotFoundException(message="Docking station not found")
            if station.robot_id is not None:
                raise BadRequestException(
                    message="Docking station already has a robot assigned",
                    status_code=409,
                )
            robot = await self.repo.bot_repo().get_by_id_and_group_for_update(
                session=session, bot_id=robot_id, group_id=group_id, ctx=ctx
            )
            if robot is None:
                raise NotFoundException(message="Robot not found")
            if robot.map_id != map_id:
                raise BadRequestException(message="Robot must be assigned to this map")
            if await repository.robot_has_station(
                session=session, robot_id=robot_id, ctx=ctx
            ):
                raise BadRequestException(
                    message="Robot already has a docking station", status_code=409
                )
            try:
                assigned = await repository.assign_robot_if_empty(
                    session=session,
                    map_id=map_id,
                    docking_id=docking_id,
                    robot_id=robot_id,
                    ctx=ctx,
                )
            except DockingStationRobotConflict as error:
                raise BadRequestException(
                    message="Robot already has a docking station", status_code=409
                ) from error
            if not assigned:
                raise BadRequestException(
                    message="Docking station changed; refresh the map and try again",
                    status_code=409,
                )

        await self.repo.transaction_wrapper(assign)

    @require_permission(GroupRole.ADMIN)
    async def save_docking_stations(
        self,
        station_save: DockingStationsSaveDTO,
        group_id: UUID | None,
        credential: Credential,
        ctx: AppContext,
    ) -> list[DockingStationInfo]:
        """Save the complete station snapshot atomically under the map lock."""
        self._validate_active_group(group_id, credential)

        async def save(session: AsyncSession) -> list[DockingStationInfo]:
            await self._lock_map(session, station_save.map_id, group_id, ctx)
            return await self.save_docking_stations_in_transaction(
                session=session,
                map_id=station_save.map_id,
                stations=station_save.data,
                group_id=group_id,
                ctx=ctx,
            )

        results = await self.repo.transaction_wrapper(save)
        logger.info(
            msg=f"Saved {len(results)} docking stations for map {station_save.map_id}",
            context=ctx,
        )
        return results

    @staticmethod
    def _validate_active_group(group_id: UUID | None, credential: Credential) -> None:
        if group_id is None or group_id != credential.active_group_id:
            raise ForbiddenException(message="An active group must be selected")

    async def save_docking_stations_in_transaction(
        self,
        session: AsyncSession,
        map_id: UUID,
        stations: list[DockingStationSaveItemDTO],
        group_id: UUID,
        ctx: AppContext,
    ) -> list[DockingStationInfo]:
        """Save a station snapshot using a caller-owned transaction and map lock."""
        request = DockingStationsSaveDTO(map_id=map_id, data=stations)
        current = await self.repo.docking_station_repo().list_for_map(
            session=session,
            map_id=map_id,
            ctx=ctx,
        )
        creates, edits, deletes = self._partition_actions(request.data, current)
        self._validate_edit_ids(edits, current)
        await self._validate_actions(session, request, group_id, ctx)
        updated: list[tuple[int, DockingStationInfo]] = []
        created: list[tuple[int, DockingStationInfo]] = []
        if deletes:
            await self._delete_stations(session, request.map_id, deletes, ctx)
        if edits:
            await self._release_changed_assignments(
                session, request.map_id, edits, current, ctx
            )
            updated = await self._edit_stations(session, request.map_id, edits, ctx)
        if creates:
            created = await self._create_stations(session, request.map_id, creates, ctx)
        return [
            station
            for _, station in sorted([*updated, *created], key=lambda row: row[0])
        ]

    async def validate_all_within_boundary(
        self,
        session: AsyncSession,
        map_id: UUID,
        ctx: AppContext,
    ) -> None:
        """Reject a final layout containing a station outside its boundary."""
        if await self.repo.docking_station_repo().has_outside_boundary(
            session=session,
            map_id=map_id,
            ctx=ctx,
        ):
            raise BadRequestException(
                message="All docking stations must be within the map boundary"
            )

    async def _lock_map(
        self, session: AsyncSession, map_id: UUID, group_id: UUID, ctx: AppContext
    ) -> None:
        record = await self.repo.map_repo().get_by_id_and_group_for_update(
            session=session,
            map_id=map_id,
            group_id=group_id,
            ctx=ctx,
        )
        if record is None:
            raise NotFoundException(message="Map not found")

    @staticmethod
    def _partition_actions(
        items: list[DockingStationSaveItemDTO],
        current: dict[UUID, UUID | None],
    ) -> tuple[list[IndexedStation], list[IndexedStation], list[UUID]]:
        creates = [(index, item) for index, item in enumerate(items) if item.id is None]
        edits = [
            (index, item) for index, item in enumerate(items) if item.id is not None
        ]
        retained = {item.id for _, item in edits}
        # Deletions can only target rows loaded from the authorized, locked map.
        return creates, edits, list(current.keys() - retained)

    @staticmethod
    def _validate_edit_ids(
        edits: list[IndexedStation], current: dict[UUID, UUID | None]
    ) -> None:
        for index, item in edits:
            if item.id not in current:
                raise BadRequestException(
                    message=f"Station at index {index} was changed or deleted; refresh the list and try again",
                    status_code=409,
                )

    async def _validate_actions(
        self,
        session: AsyncSession,
        request: DockingStationsSaveDTO,
        group_id: UUID,
        ctx: AppContext,
    ) -> None:
        """Fetch validation facts once for the whole batch, before any mutation."""
        robot_ids = sorted(
            {item.robot_id for item in request.data if item.robot_id is not None}
        )
        robots = {
            robot.id: robot
            for robot in await self.repo.bot_repo().get_by_ids_and_group_for_update(
                session=session,
                bot_ids=robot_ids,
                group_id=group_id,
                ctx=ctx,
            )
        }
        repository = self.repo.docking_station_repo()
        inspections = await repository.inspect_boundary_placement(
            session=session,
            map_id=request.map_id,
            geometry_jsons=[item.geometry.model_dump_json() for item in request.data],
            ctx=ctx,
        )
        conflicts = await repository.robots_with_other_map_stations(
            session=session,
            robot_ids=robot_ids,
            map_id=request.map_id,
            ctx=ctx,
        )
        # Validate in request order even though writes are grouped by action.
        for inspection in inspections:
            index = inspection.item_index
            item = request.data[index]
            self._validate_geometry(inspection)
            # Persist the translated polygon returned by PostGIS so clients see
            # the same snapped geometry that was validated.
            if inspection.snapped_geometry is not None:
                item.geometry = PolygonGeometry.model_validate(
                    inspection.snapped_geometry
                )
            self._validate_robot(
                index, item.robot_id, request.map_id, robots, conflicts
            )

    @staticmethod
    def _validate_geometry(inspection: BoundaryPlacementInspection) -> None:
        index = inspection.item_index
        if not inspection.boundary_exists:
            raise BadRequestException(
                message="A map boundary is required before saving stations"
            )
        if not inspection.geometry_valid:
            raise BadRequestException(
                message=(
                    f"Docking station {index + 1} must have a valid, non-empty shape"
                )
            )
        if not inspection.edge_aligned:
            raise BadRequestException(
                message=(
                    f"Docking station {index + 1} must have one side touching or "
                    "within 0.5 map units of the workspace edge"
                )
            )
        if not inspection.fully_covered and not inspection.protrusion_allowed:
            raise BadRequestException(
                message=(
                    f"Docking station {index + 1} must stay inside the workspace, "
                    "except for up to 0.5 map units along the aligned edge"
                )
            )

    @staticmethod
    def _validate_robot(
        index: int,
        robot_id: UUID | None,
        map_id: UUID,
        robots: dict[UUID, Robot],
        conflicts: set[UUID],
    ) -> None:
        if robot_id is None:
            return
        robot = robots.get(robot_id)
        if robot is None:
            raise NotFoundException(
                message=f"Robot for station at index {index} not found"
            )
        if robot.map_id != map_id:
            raise BadRequestException(
                message=f"Robot for station at index {index} must be assigned to this map"
            )
        if robot.id in conflicts:
            raise BadRequestException(
                message=f"Robot for station at index {index} already has a docking station on another map",
                status_code=409,
            )

    async def _delete_stations(
        self,
        session: AsyncSession,
        map_id: UUID,
        station_ids: list[UUID],
        ctx: AppContext,
    ) -> None:
        await self.repo.docking_station_repo().delete_many(
            session=session,
            map_id=map_id,
            station_ids=station_ids,
            ctx=ctx,
        )

    async def _release_changed_assignments(
        self,
        session: AsyncSession,
        map_id: UUID,
        edits: list[IndexedStation],
        current: dict[UUID, UUID | None],
        ctx: AppContext,
    ) -> None:
        """Release assignments across all edits before writing either action batch."""
        changed = [item.id for _, item in edits if current[item.id] != item.robot_id]
        await self.repo.docking_station_repo().clear_assignments(
            session=session,
            map_id=map_id,
            station_ids=changed,
            ctx=ctx,
        )

    async def _create_stations(
        self,
        session: AsyncSession,
        map_id: UUID,
        items: list[IndexedStation],
        ctx: AppContext,
    ) -> list[tuple[int, DockingStationInfo]]:
        return await self._write_stations(session, map_id, items, ctx)

    async def _edit_stations(
        self,
        session: AsyncSession,
        map_id: UUID,
        items: list[IndexedStation],
        ctx: AppContext,
    ) -> list[tuple[int, DockingStationInfo]]:
        return await self._write_stations(session, map_id, items, ctx)

    async def _write_stations(
        self,
        session: AsyncSession,
        map_id: UUID,
        items: list[IndexedStation],
        ctx: AppContext,
    ) -> list[tuple[int, DockingStationInfo]]:
        if not items:
            return []
        try:
            rows = await self.repo.docking_station_repo().save_many(
                session=session,
                map_id=map_id,
                stations=[
                    (
                        item.id,
                        item.robot_id,
                        item.heading or DockingStationHeading.SOUTH,
                        item.geometry.model_dump_json(),
                    )
                    for _, item in items
                ],
                ctx=ctx,
            )
        except DockingStationRobotConflict as error:
            location = (
                f" for station at index {items[error.index][0]}"
                if error.index is not None
                else ""
            )
            raise BadRequestException(
                message=f"Robot{location} already has a docking station",
                status_code=409,
            ) from None
        return [
            (index, DockingStationInfo.model_validate(row))
            for (index, _), row in zip(items, rows)
        ]
