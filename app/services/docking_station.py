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
from app.common.schemas.map import DockingStationCreateDTO, DockingStationInfo
from app.common.schemas.user import Credential
from app.core.rbac.permissions import PermissionService
from app.core.rbac.role_validation import require_permission
from app.repository.docking_station import DockingStationRobotConflict
from app.repository.registry import Registry

logger = Logger()


class DockingStationService:
    def __init__(self, repo: Registry, permission_service: PermissionService) -> None:
        self.repo = repo
        self.permission_service = permission_service

    @require_permission(GroupRole.ADMIN)
    async def create_docking_station(
        self,
        map_id: UUID,
        station_create: DockingStationCreateDTO,
        group_id: UUID | None,
        credential: Credential,
        ctx: AppContext,
    ) -> DockingStationInfo:
        if group_id is None or group_id != credential.active_group_id:
            raise ForbiddenException(message="An active group must be selected")

        async def _create(session: AsyncSession) -> DockingStationInfo:
            map_record = await self.repo.map_repo().get_by_id_and_group_for_update(
                session=session,
                map_id=map_id,
                group_id=group_id,
                ctx=ctx,
            )
            if map_record is None:
                raise NotFoundException(message="Map not found")
            repository = self.repo.docking_station_repo()
            geometry_json = station_create.geometry.model_dump_json()
            boundary_exists, valid, covered = (
                await repository.inspect_boundary_coverage(
                    session=session,
                    map_id=map_id,
                    geometry_json=geometry_json,
                    ctx=ctx,
                )
            )
            if not boundary_exists:
                raise BadRequestException(
                    message="A map boundary is required before creating stations"
                )
            if not valid:
                raise BadRequestException(
                    message="Station geometry must be a valid, nonempty polygon"
                )
            if not covered:
                raise BadRequestException(
                    message="Station geometry must be within the map boundary"
                )
            if station_create.robot_id is not None:
                robot = await self.repo.bot_repo().get_by_id_and_group_for_update(
                    session=session,
                    bot_id=station_create.robot_id,
                    group_id=group_id,
                    ctx=ctx,
                )
                if robot is None:
                    raise NotFoundException(message="Robot not found")
                if robot.map_id != map_id:
                    raise BadRequestException(
                        message="Robot must be assigned to this map"
                    )
                if await repository.robot_has_station(
                    session=session, robot_id=robot.id, ctx=ctx
                ):
                    raise BadRequestException(
                        message="Robot already has a docking station", status_code=409
                    )
            row = await repository.create(
                session=session,
                map_id=map_id,
                robot_id=station_create.robot_id,
                heading=station_create.heading or DockingStationHeading.SOUTH,
                geometry_json=geometry_json,
                ctx=ctx,
            )
            return DockingStationInfo.model_validate(row)

        try:
            result = await self.repo.transaction_wrapper(_create)
        except DockingStationRobotConflict:
            raise BadRequestException(
                message="Robot already has a docking station", status_code=409
            ) from None
        logger.info(
            msg=f"Created docking station {result.id} for map {map_id}", context=ctx
        )
        return result
