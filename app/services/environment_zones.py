from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.context import AppContext
from app.common.enum.user_roles import GroupRole
from app.common.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
)
from app.common.middleware.logger import Logger
from app.common.schemas.map import EnvironmentZonesSaveDTO
from app.common.schemas.user import Credential
from app.core.rbac.permissions import PermissionService
from app.core.rbac.role_validation import require_permission
from app.repository.registry import Registry

logger = Logger()


class EnvironmentZonesService:
    """Coordinate authorized creation of map environment zones."""

    def __init__(self, repo: Registry, permission_service: PermissionService) -> None:
        self.repo = repo
        self.permission_service = permission_service

    @require_permission(GroupRole.ADMIN)
    async def save_environment_zones(
        self,
        zones_create: EnvironmentZonesSaveDTO,
        group_id: UUID | None,
        credential: Credential,
        ctx: AppContext,
    ) -> None:
        """Atomically append contained, mutually non-overlapping map zones.

        Authorization is enforced by ``require_permission`` before this method
        runs. The complete validation and insert workflow then executes in one
        transaction while holding a row lock on the parent map. Boundary saves
        and competing zone writes acquire the same lock, so a concurrent request
        validates against the zones committed by the first successful writer.
        """
        # Never accept a group supplied independently of the authenticated context.
        if group_id is None or group_id != credential.active_group_id:
            raise ForbiddenException(message="An active group must be selected")

        async def _create(session: AsyncSession) -> None:
            # Scope the map lookup to the active group and serialize writes per map.
            map_record = await self.repo.map_repo().get_by_id_and_group_for_update(
                session=session,
                map_id=zones_create.map_id,
                group_id=group_id,
                ctx=ctx,
            )
            if map_record is None:
                raise NotFoundException(message="Map not found")

            repository = self.repo.environment_zone_repo()
            geometry_jsons = [
                zone.geometry.model_dump_json() for zone in zones_create.zones
            ]

            boundary_exists, inspections = await repository.inspect_boundary_coverage(
                session=session,
                map_id=map_record.id,
                geometry_jsons=geometry_jsons,
                ctx=ctx,
            )
            if not boundary_exists:
                raise BadRequestException(
                    message="A map boundary is required before creating zones"
                )
            for index, valid, covered in inspections:
                if not valid:
                    raise BadRequestException(
                        message=f"Zone at index {index} must be a valid, nonempty polygon"
                    )
                if not covered:
                    raise BadRequestException(
                        message=f"Zone at index {index} must be within the map boundary"
                    )

            overlap = await repository.find_batch_overlap(
                session=session, geometry_jsons=geometry_jsons, ctx=ctx
            )
            if overlap is not None:
                raise BadRequestException(
                    message=f"Zones at indexes {overlap[0]} and {overlap[1]} overlap"
                )

            existing_overlap = await repository.find_existing_overlap(
                session=session,
                map_id=map_record.id,
                geometry_jsons=geometry_jsons,
                ctx=ctx,
            )
            if existing_overlap is not None:
                raise BadRequestException(
                    message=f"Zone at index {existing_overlap} overlaps an existing zone"
                )

            await repository.create_many(
                session=session,
                map_id=map_record.id,
                zones=[
                    (zone.type, geometry_json)
                    for zone, geometry_json in zip(
                        zones_create.zones, geometry_jsons, strict=True
                    )
                ],
                ctx=ctx,
            )
            logger.info(
                msg=f"Created {len(zones_create.zones)} zones for map {map_record.id}",
                context=ctx,
            )

        await self.repo.transaction_wrapper(_create)
