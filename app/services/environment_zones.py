from uuid import UUID

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.context import AppContext
from app.common.enum.user_roles import GroupRole
from app.common.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
)
from app.common.middleware.logger import Logger
from app.common.schemas.map import EnvironmentZoneSaveItemDTO, EnvironmentZonesSaveDTO
from app.common.schemas.user import Credential
from app.core.rbac.permissions import PermissionService
from app.core.rbac.role_validation import require_permission
from app.repository.environment_zone import EnvironmentZoneRepository
from app.repository.registry import Registry

logger = Logger()


class EnvironmentZonesService:
    """Coordinate authorized atomic saves of map environment zones."""

    def __init__(self, repo: Registry, permission_service: PermissionService) -> None:
        self.repo = repo
        self.permission_service = permission_service

    @staticmethod
    def _partition_zone_items(
        zones: list[EnvironmentZoneSaveItemDTO],
    ) -> tuple[
        list[tuple[int, EnvironmentZoneSaveItemDTO]],
        list[tuple[int, EnvironmentZoneSaveItemDTO]],
        list[tuple[int, EnvironmentZoneSaveItemDTO]],
        list[UUID],
    ]:
        """Split request items and collect IDs that require a database lookup."""
        delete_items: list[tuple[int, EnvironmentZoneSaveItemDTO]] = []
        create_items: list[tuple[int, EnvironmentZoneSaveItemDTO]] = []
        edit_items: list[tuple[int, EnvironmentZoneSaveItemDTO]] = []

        for index, zone in enumerate(zones):
            if zone.to_delete:
                delete_items.append((index, zone))
            elif zone.id is None:
                create_items.append((index, zone))
            else:
                edit_items.append((index, zone))

        submitted_ids = [
            zone.id for _, zone in [*delete_items, *edit_items] if zone.id is not None
        ]

        return delete_items, create_items, edit_items, submitted_ids

    @staticmethod
    async def _validate_boundary_coverage(
        repository: EnvironmentZoneRepository,
        session: AsyncSession,
        map_id: UUID,
        geometry_jsons: list[str],
        validation_items: list[tuple[int, EnvironmentZoneSaveItemDTO]],
        ctx: AppContext,
    ) -> None:
        """Validate candidate polygons against the map's current boundary."""
        boundary_exists, inspections = await repository.inspect_boundary_coverage(
            session=session,
            map_id=map_id,
            geometry_jsons=geometry_jsons,
            ctx=ctx,
        )
        if not boundary_exists:
            raise BadRequestException(
                message="A map boundary is required before saving zones"
            )
        for validation_index, valid, covered in inspections:
            request_index = validation_items[validation_index][0]
            if not valid:
                raise BadRequestException(
                    message=(
                        f"Zone at index {request_index} must be a valid, "
                        "nonempty polygon"
                    )
                )
            if not covered:
                raise BadRequestException(
                    message=(
                        f"Zone at index {request_index} must be within "
                        "the map boundary"
                    )
                )

    @staticmethod
    async def _validate_overlaps(
        repository: EnvironmentZoneRepository,
        session: AsyncSession,
        map_id: UUID,
        geometry_jsons: list[str],
        validation_items: list[tuple[int, EnvironmentZoneSaveItemDTO]],
        excluded_zone_ids: list[UUID],
        ctx: AppContext,
    ) -> None:
        """Reject candidate polygons that overlap each other or retained zones."""
        overlap = await repository.find_batch_overlap(
            session=session, geometry_jsons=geometry_jsons, ctx=ctx
        )
        if overlap is not None:
            raise BadRequestException(
                message=(
                    "Zones at indexes "
                    f"{validation_items[overlap[0]][0]} and "
                    f"{validation_items[overlap[1]][0]} overlap"
                )
            )

        existing_overlap = await repository.find_existing_overlap(
            session=session,
            map_id=map_id,
            geometry_jsons=geometry_jsons,
            ctx=ctx,
            excluded_zone_ids=excluded_zone_ids,
        )
        if existing_overlap is not None:
            raise BadRequestException(
                message=(
                    f"Zone at index {validation_items[existing_overlap][0]} "
                    "overlaps an existing zone"
                )
            )

    @require_permission(GroupRole.ADMIN)
    async def save_environment_zones(
        self,
        zones_create: EnvironmentZonesSaveDTO,
        group_id: UUID | None,
        credential: Credential,
        ctx: AppContext,
    ) -> None:
        """Atomically create, update, and delete map environment zones.

        Authorization is enforced by ``require_permission`` before this method
        runs. The complete validation and persistence workflow then executes in one
        transaction while holding a row lock on the parent map. Boundary saves
        and competing zone writes acquire the same lock, so a concurrent request
        validates against the zones committed by the first successful writer.
        """
        # Never accept a group supplied independently of the authenticated context.
        if group_id is None or group_id != credential.active_group_id:
            raise ForbiddenException(message="An active group must be selected")

        async def _save(session: AsyncSession) -> None:
            # Scope the map lookup to the active group and serialize writes per map.
            map_record = await self.repo.map_repo().get_by_id_and_group_for_update(
                session=session,
                map_id=zones_create.map_id,
                group_id=group_id,
                ctx=ctx,
            )
            if map_record is None:
                raise NotFoundException(message="Map not found")

            await self.save_environment_zones_in_transaction(
                session=session,
                map_id=map_record.id,
                zones=zones_create.zones,
                ctx=ctx,
            )

        await self.repo.transaction_wrapper(_save)
        logger.info(
            msg=f"Saved environment zones for map {zones_create.map_id}",
            context=ctx,
        )

    async def save_environment_zones_in_transaction(
        self,
        session: AsyncSession,
        map_id: UUID,
        zones: list[EnvironmentZoneSaveItemDTO],
        ctx: AppContext,
    ) -> None:
        """Apply zone changes using a caller-owned transaction and map lock."""
        repository = self.repo.environment_zone_repo()
        if not zones:
            await repository.delete_all_for_map(
                session=session,
                map_id=map_id,
                ctx=ctx,
            )
            return

        (
            delete_items,
            create_items,
            edit_items,
            submitted_ids,
        ) = self._partition_zone_items(zones)

        existing_ids = await repository.get_existing_ids(
            session=session,
            map_id=map_id,
            zone_ids=submitted_ids,
            ctx=ctx,
        )
        missing_edit = next(
            (index for index, zone in edit_items if zone.id not in existing_ids),
            None,
        )
        if missing_edit is not None:
            raise BadRequestException(
                message=(
                    f"Zone at index {missing_edit} was changed or deleted; "
                    "refresh the list and try again"
                ),
                status_code=status.HTTP_409_CONFLICT,
            )

        validation_items = [*create_items, *edit_items]
        geometry_jsons = [
            zone.geometry.model_dump_json() for _, zone in validation_items
        ]
        if validation_items:
            await self._validate_boundary_coverage(
                repository=repository,
                session=session,
                map_id=map_id,
                geometry_jsons=geometry_jsons,
                validation_items=validation_items,
                ctx=ctx,
            )

            await self._validate_overlaps(
                repository=repository,
                session=session,
                map_id=map_id,
                geometry_jsons=geometry_jsons,
                validation_items=validation_items,
                excluded_zone_ids=submitted_ids,
                ctx=ctx,
            )

        delete_ids = [
            zone.id
            for _, zone in delete_items
            if zone.id is not None and zone.id in existing_ids
        ]
        if delete_ids:
            await repository.delete_many(
                session=session,
                map_id=map_id,
                zone_ids=delete_ids,
                ctx=ctx,
            )
        if edit_items:
            await repository.update_many(
                session=session,
                map_id=map_id,
                zones=[
                    (zone.id, zone.type, zone.geometry.model_dump_json())
                    for _, zone in edit_items
                    if zone.id is not None
                ],
                ctx=ctx,
            )
        if create_items:
            await repository.create_many(
                session=session,
                map_id=map_id,
                zones=[
                    (zone.type, zone.geometry.model_dump_json())
                    for _, zone in create_items
                ],
                ctx=ctx,
            )

    async def validate_all_within_boundary(
        self,
        session: AsyncSession,
        map_id: UUID,
        ctx: AppContext,
    ) -> None:
        """Reject a final layout containing a zone outside its boundary."""
        if await self.repo.environment_zone_repo().has_outside_boundary(
            session=session,
            map_id=map_id,
            ctx=ctx,
        ):
            raise BadRequestException(
                message="All environment zones must be within the map boundary"
            )
