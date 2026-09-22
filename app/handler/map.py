from uuid import UUID, uuid4

from fastapi import Depends, Query

from app.common.context import AppContext
from app.common.enum.context_actions import (
    SAVE_DOCKING_STATIONS,
    CREATE_ENVIRONMENT_ZONES,
    CREATE_MAP,
    LIST_MAPS,
    SAVE_MAP_BOUNDARY,
)
from app.common.exceptions.decorator import exception_handler
from app.common.middleware.auth_middleware import AuthMiddleware
from app.common.schemas.common import BaseResponse, PaginationBaseResponse
from app.common.schemas.map import (
    DockingStationsSaveDTO,
    DockingStationInfo,
    MapBoundaryInfo,
    MapBoundarySaveDTO,
    MapCreateDTO,
    MapDetailInfo,
    EnvironmentZonesSaveDTO,
    MapInfo,
    MapListInfo,
    MapListQuery,
)
from app.common.schemas.user import Credential
from app.services.docking_station import DockingStationService
from app.services.map import MapService
from app.services.environment_zones import EnvironmentZonesService


class MapHandler:
    def __init__(
        self,
        service: MapService,
        environment_zones_service: EnvironmentZonesService,
        docking_station_service: DockingStationService,
    ) -> None:
        self.docking_station_service = docking_station_service
        self.service = service
        self.environment_zones_service = environment_zones_service

    @exception_handler
    async def save_docking_stations(
        self,
        station_save: DockingStationsSaveDTO,
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> BaseResponse[list[DockingStationInfo]]:
        ctx = AppContext(
            trace_id=uuid4(), action=SAVE_DOCKING_STATIONS, actor=credential.id
        )
        stations = await self.docking_station_service.save_docking_stations(
            station_save=station_save,
            group_id=credential.active_group_id,
            credential=credential,
            ctx=ctx,
        )
        return BaseResponse[list[DockingStationInfo]](
            data=stations,
            message="Docking stations saved",
            statusCode=200,
        )

    @exception_handler
    async def save_environment_zones(
        self,
        zones_create: EnvironmentZonesSaveDTO,
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> None:
        ctx = AppContext(
            trace_id=uuid4(), action=CREATE_ENVIRONMENT_ZONES, actor=credential.id
        )
        await self.environment_zones_service.save_environment_zones(
            zones_create=zones_create,
            group_id=credential.active_group_id,
            credential=credential,
            ctx=ctx,
        )

    @exception_handler
    async def save_boundary(
        self,
        boundary_save: MapBoundarySaveDTO,
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> BaseResponse[MapBoundaryInfo]:
        ctx = AppContext(
            trace_id=uuid4(), action=SAVE_MAP_BOUNDARY, actor=credential.id
        )
        boundary = await self.service.save_boundary(
            boundary_save=boundary_save,
            group_id=credential.active_group_id,
            credential=credential,
            ctx=ctx,
        )
        return BaseResponse[MapBoundaryInfo](
            data=boundary,
            message="Map boundary saved",
            statusCode=200,
        )

    @exception_handler
    async def create_map(
        self,
        map_create: MapCreateDTO,
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> BaseResponse[MapInfo]:
        ctx = AppContext(trace_id=uuid4(), action=CREATE_MAP, actor=credential.id)
        map_info = await self.service.create_map(
            map_create=map_create,
            group_id=map_create.group_id,
            credential=credential,
            ctx=ctx,
        )
        return BaseResponse[MapInfo](
            data=map_info,
            message="Map created",
            statusCode=201,
        )

    @exception_handler
    async def get_map_detail(
        self,
        map_id: UUID,
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> BaseResponse[MapDetailInfo]:
        ctx = AppContext(trace_id=uuid4(), action=LIST_MAPS, actor=credential.id)
        map_detail = await self.service.get_map_detail(
            map_id=map_id,
            group_id=credential.active_group_id,
            credential=credential,
            ctx=ctx,
        )
        return BaseResponse[MapDetailInfo](
            data=map_detail, message="Map retrieved", statusCode=200
        )

    @exception_handler
    async def list_maps(
        self,
        query: MapListQuery = Query(),
        credential: Credential = Depends(AuthMiddleware.auth_middleware),
    ) -> PaginationBaseResponse[MapListInfo]:
        ctx = AppContext(trace_id=uuid4(), action=LIST_MAPS, actor=credential.id)
        maps, total = await self.service.list_maps(
            query=query,
            group_id=credential.active_group_id,
            credential=credential,
            ctx=ctx,
        )
        return PaginationBaseResponse[MapListInfo](
            total=total,
            page=query.page,
            page_size=query.page_size,
            items=maps,
        )
