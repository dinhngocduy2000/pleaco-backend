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
from app.common.schemas.map import (
    DockingStationInfo,
    DockingStationSaveItemDTO,
    DockingStationsSaveDTO,
)
from app.common.schemas.user import Credential
from app.core.rbac.permissions import PermissionService
from app.core.rbac.role_validation import require_permission
from app.models.robot import Robot
from app.repository.docking_station import DockingStationRobotConflict
from app.repository.registry import Registry

logger = Logger()
IndexedStation = tuple[int, DockingStationSaveItemDTO]


class DockingStationService:
    def __init__(self, repo: Registry, permission_service: PermissionService) -> None:
        self.repo = repo
        self.permission_service = permission_service

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
        await self._delete_stations(session, request.map_id, deletes, ctx)
        await self._release_changed_assignments(
            session, request.map_id, edits, current, ctx
        )
        updated = await self._edit_stations(session, request.map_id, edits, ctx)
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
        inspections = await repository.inspect_boundary_coverage(
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
        for index, (item, inspection) in enumerate(zip(request.data, inspections)):
            self._validate_geometry(index, inspection)
            self._validate_robot(
                index, item.robot_id, request.map_id, robots, conflicts
            )

    @staticmethod
    def _validate_geometry(index: int, inspection: tuple[bool, bool, bool]) -> None:
        boundary_exists, valid, covered = inspection
        if not boundary_exists:
            raise BadRequestException(
                message="A map boundary is required before saving stations"
            )
        if not valid:
            raise BadRequestException(
                message=f"Station at index {index} must be a valid, nonempty polygon"
            )
        if not covered:
            raise BadRequestException(
                message=f"Station at index {index} must be within the map boundary"
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
