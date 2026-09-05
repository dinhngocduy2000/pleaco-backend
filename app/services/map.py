from math import isfinite
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.context import AppContext
from app.common.enum.geometry import GeometryType
from app.common.enum.map import MapBoundarySource, MapStatus
from app.common.enum.user_roles import GroupRole
from app.common.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
)
from app.common.middleware.logger import Logger
from app.common.schemas.geometry import PolygonGeometry
from app.common.schemas.map import (
    MapBoundaryInfo,
    MapBoundarySaveDTO,
    MapCreateDTO,
    MapInfo,
    MapListInfo,
    MapListQuery,
)
from app.common.schemas.tags import TagInfo, TagListInfo
from app.common.schemas.user import Credential
from app.core.rbac.permissions import PermissionService
from app.core.rbac.role_validation import require_permission
from app.models.map import Map
from app.models.robot import Robot
from app.models.tag import Tag
from app.repository.registry import Registry

logger = Logger()


class MapService:
    """Coordinate authorized map creation and optional resource assignment."""

    def __init__(self, repo: Registry, permission_service: PermissionService) -> None:
        self.repo = repo
        self.permission_service = permission_service

    @require_permission(GroupRole.ADMIN)
    async def save_boundary(
        self,
        boundary_save: MapBoundarySaveDTO,
        group_id: UUID | None,
        credential: Credential,
        ctx: AppContext,
    ) -> MapBoundaryInfo:
        """Create or replace an active-group map's boundary in one transaction."""
        if group_id is None or group_id != credential.active_group_id:
            raise ForbiddenException(message="An active group must be selected")

        async def _save(session: AsyncSession) -> MapBoundaryInfo:
            map_record = await self.repo.map_repo().get_by_id_and_group_for_update(
                session=session,
                map_id=boundary_save.map_id,
                group_id=group_id,
                ctx=ctx,
            )
            if map_record is None:
                raise NotFoundException(message="Map not found")

            try:
                x, y = float(map_record.dimension_x), float(map_record.dimension_y)
            except (ValueError, OverflowError):
                raise BadRequestException(
                    message="Map dimensions must be positive finite numbers"
                ) from None
            if not all(isfinite(value) and value > 0 for value in (x, y)):
                raise BadRequestException(
                    message="Map dimensions must be positive finite numbers"
                )

            geometry = boundary_save.geometry
            if boundary_save.source == MapBoundarySource.DIMENSIONS:
                geometry = self._dimension_boundary(x, y)
            if geometry is None:
                raise BadRequestException(
                    message="Geometry is required for CUSTOM and TEACH_MODE"
                )
            geometry_json = geometry.model_dump_json()
            repository = self.repo.map_boundary_repo()
            valid, covered = await repository.inspect_geometry(
                session=session,
                geometry_json=geometry_json,
                dimension_x=x,
                dimension_y=y,
                ctx=ctx,
            )
            if not valid:
                raise BadRequestException(message="Boundary must be a valid, nonempty polygon")
            if not covered:
                raise BadRequestException(message="Boundary must be within the map dimensions")

            result = MapBoundaryInfo.model_validate(
                await repository.upsert(
                    session=session,
                    map_id=map_record.id,
                    source=boundary_save.source,
                    geometry_json=geometry_json,
                    ctx=ctx,
                )
            )
            logger.info(
                msg=f"Saved boundary {result.id} for map {map_record.id} with source {result.source.value}",
                context=ctx,
            )
            return result

        return await self.repo.transaction_wrapper(_save)

    @staticmethod
    def _dimension_boundary(x: float, y: float) -> PolygonGeometry:
        return PolygonGeometry(
            type=GeometryType.POLYGON,
            coordinates=[[(0, 0), (x, 0), (x, y), (0, y), (0, 0)]],
        )

    @require_permission(GroupRole.ADMIN)
    async def create_map(
        self,
        map_create: MapCreateDTO,
        group_id: UUID,
        credential: Credential,
        ctx: AppContext,
    ) -> MapInfo:
        """Create a group map and atomically attach its requested resources.

        The caller must be an Owner or Admin of ``group_id``. All requested tags
        and robots are group-scoped; robots must exist and be unassigned before
        they can be linked to the newly created map.

        Args:
            map_create: Validated map attributes and optional robot/tag IDs.
            group_id: Identifier of the group that will own the map.
            credential: Authenticated caller used by the RBAC decorator.
            ctx: Request trace context used for logging and repository calls.

        Returns:
            The created map and its assigned robot IDs and tags.

        Raises:
            BadRequestException: If the map name already exists in the group or
                a requested robot is already assigned.
            NotFoundException: If any requested tag or robot is outside the
                group or does not exist.
        """
        logger.info(
            msg=(
                f"Creating map '{map_create.name}' for group {group_id} "
                f"with {len(map_create.robot_ids)} robots and "
                f"{len(map_create.tags)} tags"
            ),
            context=ctx,
        )

        async def _create_map(session: AsyncSession) -> MapInfo:
            map_repository = self.repo.map_repo()
            bot_repository = self.repo.bot_repo()
            await self._validate_map_name(
                session=session,
                group_id=group_id,
                name=map_create.name,
                ctx=ctx,
            )
            tags = await self._validate_tags(
                session=session,
                tag_ids=map_create.tags,
                group_id=group_id,
                ctx=ctx,
            )
            robots = await self._validate_robots(
                session=session,
                robot_ids=map_create.robot_ids,
                group_id=group_id,
                ctx=ctx,
            )

            status = MapStatus.ASSIGNED if robots else MapStatus.UNASSIGNED
            map_record = await map_repository.create_map(
                session=session,
                group_id=group_id,
                name=map_create.name,
                description=map_create.description,
                dimension_x=map_create.dimension_x,
                dimension_y=map_create.dimension_y,
                status=status,
                tags=tags,
                ctx=ctx,
            )
            await bot_repository.assign_map(
                session=session, robots=robots, map_id=map_record.id, ctx=ctx
            )
            logger.info(
                msg=(
                    f"Created map {map_record.id} for group {group_id} with "
                    f"status {status.value}, {len(robots)} robots, and {len(tags)} tags"
                ),
                context=ctx,
            )
            return self._to_map_info(map_record, map_create.robot_ids)

        return await self.repo.transaction_wrapper(_create_map)

    async def _validate_map_name(
        self, session: AsyncSession, group_id: UUID, name: str, ctx: AppContext
    ) -> None:
        """Reject a map name that is already in use by the target group."""
        existing_map = await self.repo.map_repo().get_by_group_and_name(
            session=session, group_id=group_id, name=name, ctx=ctx
        )
        if existing_map is not None:
            logger.warning(
                msg=f"Map name '{name}' already exists in group {group_id}",
                context=ctx,
            )
            raise BadRequestException(
                message="A map with this name already exists in this group"
            )

    async def _validate_tags(
        self,
        session: AsyncSession,
        tag_ids: list[UUID],
        group_id: UUID,
        ctx: AppContext,
    ) -> list[Tag]:
        """Resolve requested tags and ensure every tag belongs to the group."""
        tags = await self.repo.tag_repo().get_by_ids_and_group(
            session=session, tag_ids=tag_ids, group_id=group_id, ctx=ctx
        )
        if len(tags) != len(tag_ids):
            logger.warning(
                msg=f"Map creation references tags outside group {group_id}",
                context=ctx,
            )
            raise NotFoundException(message="One or more tags were not found")
        return tags

    async def _validate_robots(
        self,
        session: AsyncSession,
        robot_ids: list[UUID],
        group_id: UUID,
        ctx: AppContext,
    ) -> list[Robot]:
        """Lock and validate group robots before assigning the new map."""
        robots = await self.repo.bot_repo().get_by_ids_and_group_for_update(
            session=session, bot_ids=robot_ids, group_id=group_id, ctx=ctx
        )
        if len(robots) != len(robot_ids):
            logger.warning(
                msg=f"Map creation references robots outside group {group_id}",
                context=ctx,
            )
            raise NotFoundException(message="One or more robots were not found")
        if any(robot.map_id is not None for robot in robots):
            logger.warning(
                msg="Map creation includes one or more already assigned robots",
                context=ctx,
            )
            raise BadRequestException(
                message="One or more robots are already assigned to a map"
            )
        return robots

    @require_permission(GroupRole.GUEST)
    async def list_maps(
        self,
        query: MapListQuery,
        group_id: UUID | None,
        credential: Credential,
        ctx: AppContext,
    ) -> tuple[list[MapListInfo], int]:
        """Return filtered maps visible in the caller's active group only."""
        if group_id is None:
            raise ForbiddenException(message="A group must be selected")

        logger.info(
            msg=(
                f"Listing maps for group {group_id} with search={query.search!r}, "
                f"status={query.status}, and {len(query.tag_ids or [])} tag filters"
            ),
            context=ctx,
        )

        async def _list_maps(session: AsyncSession) -> tuple[list[MapListInfo], int]:
            rows, total = await self.repo.map_repo().list_maps(
                session=session, query=query, group_id=group_id, ctx=ctx
            )
            tags_by_map = await self.repo.map_tags_repo().get_by_map_ids(
                session=session,
                map_ids=[row["id"] for row in rows],
                group_id=group_id,
                ctx=ctx,
            )
            maps = [
                MapListInfo(
                    id=row["id"],
                    name=row["name"],
                    description=row["description"],
                    status=row["status"],
                    tags=[
                        TagListInfo(id=tag.id, name=tag.name, color=tag.color)
                        for tag in tags_by_map.get(row["id"], [])
                    ],
                    dimension_x=row["dimension_x"],
                    dimension_y=row["dimension_y"],
                    updated_at=row["updated_at"],
                    geometry=row["geometry"],
                )
                for row in rows
            ]
            logger.info(
                msg=f"Listed {len(maps)} maps from {total} matching maps for group {group_id}",
                context=ctx,
            )
            return maps, total

        return await self.repo.transaction_wrapper(_list_maps)

    @staticmethod
    def _to_map_info(map_record: Map, robot_ids: list[UUID]) -> MapInfo:
        """Translate a persisted map and its assignment IDs into API output."""
        return MapInfo(
            id=map_record.id,
            group_id=map_record.group_id,
            name=map_record.name,
            description=map_record.description,
            status=map_record.status,
            dimension_x=map_record.dimension_x,
            dimension_y=map_record.dimension_y,
            robot_ids=robot_ids,
            tags=[
                TagInfo(
                    id=tag.id,
                    name=tag.name,
                    color=tag.color,
                    description=tag.description,
                    created_at=tag.created_at,
                    updated_at=tag.updated_at,
                )
                for tag in map_record.tags
            ],
            created_at=map_record.created_at,
            updated_at=map_record.updated_at,
        )
