import asyncio
from contextlib import suppress
from pathlib import Path
from collections.abc import  Callable
from app.core.rbac.permissions import PermissionService
from app.external.queues.topics.base import init_topics
from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from starlette.requests import Request

from app.common.middleware import register_middleware
from app.common.middleware.auth_middleware import AuthMiddleware
from app.core.config import settings
from app.core.database import create_pg_engine
from app.external.mail.mail import MailService
from app.external.queues.queue import RabbitMQClient
from app.external.queues.topics.user_verification import UserVerificationTopic
from app.external.queues.topics.add_group_member import AddGroupMemberTopic
from app.external.queues.topics.robot_status import RobotStatusTopic
from app.external.mqtt.robot_status import RobotStatusMqttIngestion
from app.external.realtime.robot_status import RobotStatusWebSocketManager
from app.external.redis.redis import RedisClient
from app.handler.auth import AuthHandler
from app.handler.bot import BotHandler
from app.handler.group import GroupHandler
from app.handler.mail import MailHandler
from app.handler.map import MapHandler
from app.handler.tag import TagHandler
from app.handler.user import UserHandler
from app.repository.registry import Registry
from app.router.auth import AuthRouter
from app.router.bot import BotRouter
from app.router.group import GroupRouter
from app.router.mail import MailRouter
from app.router.map import MapRouter
from app.router.user import UserRouter
from app.router.realtime import RealtimeRouter
from app.router.tag import TagRouter
from app.services.auth import AuthService
from app.services.bot import BotService
from app.services.group import GroupService
from app.services.map import MapService
from app.services.tag import TagService
from app.services.user import UserService
from app.services.bot_status import BotStatusService


class App:
    application: FastAPI



    def on_init_app(self) -> Callable:
        async def start_app() -> None:
            pg_engine = create_pg_engine()
            redis_client = RedisClient()
            rabbitmq_client = RabbitMQClient()
            self.application.state.rabbitmq = rabbitmq_client
            registry = Registry(pg_engine=pg_engine, redis_client=redis_client)

            # ------------ External Service ------------
            mail_service = MailService()
            verification_topic = UserVerificationTopic(rabbitmq_client, mail_service)
            add_group_member_topic = AddGroupMemberTopic(rabbitmq_client, mail_service)

            # ------------ Service ------------
            user_service = UserService(repo=registry)
            auth_service = AuthService(
                repo=registry,
                user_service=user_service,
                mail_service=mail_service,
                verification_topic=verification_topic,
            )
            permission_service = PermissionService(repo=registry)
            websocket_manager = RobotStatusWebSocketManager()
            bot_status_service = BotStatusService(
                repo=registry, websocket_manager=websocket_manager
            )
            robot_status_topic = RobotStatusTopic(rabbitmq_client, bot_status_service)
            mqtt_ingestion = RobotStatusMqttIngestion(robot_status_topic)
            self.application.state.mqtt_ingestion = mqtt_ingestion
            await init_topics(
                consumers={
                    "verification-email": verification_topic.start_consumer,
                    "group-member-invitation-email": add_group_member_topic.start_consumer,
                    "robot-status": robot_status_topic.start_consumer,
                }
            )
            await mqtt_ingestion.start()
            group_service = GroupService(
                repo=registry,
                permission_service=permission_service,
                add_group_member_topic=add_group_member_topic,
            )
            bot_service = BotService(
                repo=registry,
                permission_service=permission_service,
            )
            tag_service = TagService(
                repo=registry,
                permission_service=permission_service,
            )
            map_service = MapService(
                repo=registry,
                permission_service=permission_service,
            )
            self.application.state.group_invitation_expiry_task = asyncio.create_task(
                group_service.run_invitation_expiry_reconciler(),
                name="group-invitation-expiry-reconciler",
            )
            AuthMiddleware.init(auth_service=auth_service)

            # ------------ Handler ------------
            user_handler = UserHandler(service=user_service)
            auth_handler = AuthHandler(service=auth_service)
            mail_handler = MailHandler(service=mail_service)
            group_handler = GroupHandler(
                service=group_service,
                auth_service=auth_service,
            )
            bot_handler = BotHandler(service=bot_service)
            tag_handler = TagHandler(service=tag_service)
            map_handler = MapHandler(service=map_service)

            # ------------ Router ------------
            user_router = UserRouter(handler=user_handler)
            auth_router = AuthRouter(handler=auth_handler)
            mail_router = MailRouter(handler=mail_handler)
            group_router = GroupRouter(handler=group_handler)
            bot_router = BotRouter(handler=bot_handler)
            tag_router = TagRouter(handler=tag_handler)
            map_router = MapRouter(handler=map_handler)
            realtime_router = RealtimeRouter(websocket_manager, permission_service)
            self.application.include_router(
                user_router.router,
                prefix=settings.API_V1_PREFIX + "/users",
                tags=["Users"],
            )
            self.application.include_router(
                auth_router.router,
                prefix=settings.API_V1_PREFIX + "/auth",
                tags=["Auth"],
            )
            self.application.include_router(
                mail_router.router,
                prefix=settings.API_V1_PREFIX + "/mail",
                tags=["Mail"],
            )
            self.application.include_router(
                group_router.router,
                prefix=settings.API_V1_PREFIX + "/groups",
                tags=["Groups"],
            )
            self.application.include_router(
                bot_router.router,
                prefix=settings.API_V1_PREFIX + "/bots",
                tags=["Bots"],
            )
            self.application.include_router(
                tag_router.router,
                prefix=settings.API_V1_PREFIX + "/tags",
                tags=["Tags"],
            )
            self.application.include_router(
                map_router.router,
                prefix=settings.API_V1_PREFIX + "/maps",
                tags=["Maps"],
            )
            self.application.include_router(
                realtime_router.router,
                prefix=settings.API_V1_PREFIX,
            )

        return start_app

    def on_terminate_app(self) -> Callable:
        @logger.catch
        async def stop_app() -> None:
            expiry_task = getattr(
                self.application.state, "group_invitation_expiry_task", None
            )
            if expiry_task is not None:
                expiry_task.cancel()
                with suppress(asyncio.CancelledError):
                    await expiry_task
            mqtt_ingestion = getattr(self.application.state, "mqtt_ingestion", None)
            if mqtt_ingestion is not None:
                await mqtt_ingestion.stop()
            rabbitmq_client = getattr(self.application.state, "rabbitmq", None)
            if rabbitmq_client is not None:
                await rabbitmq_client.close()

        return stop_app

    def __init__(self) -> None:
        self.application = FastAPI(**settings.fastapi_kwargs)
        repo_root = Path(__file__).resolve().parents[2]
        public_static = repo_root / "static" / "public"
        public_static.mkdir(parents=True, exist_ok=True)
        swagger_static = repo_root / "static" / "swagger-ui"
        swagger_js = swagger_static / "swagger-ui-bundle.js"
        swagger_css = swagger_static / "swagger-ui.css"
        swagger_local = swagger_js.is_file() and swagger_css.is_file()
        if swagger_local:
            self.application.mount(
                "/swagger-ui",
                StaticFiles(directory=str(swagger_static)),
                name="swagger_ui",
            )
        self.application.mount(
            "/public",
            StaticFiles(directory=str(public_static)),
            name="public",
        )

        async def swagger_ui_html(request: Request) -> HTMLResponse:
            root_path = request.scope.get("root_path", "").rstrip("/")
            openapi_url = root_path + f"{settings.API_V1_PREFIX}/openapi.json"
            if swagger_local:
                js_url = f"{root_path}/swagger-ui/swagger-ui-bundle.js"
                css_url = f"{root_path}/swagger-ui/swagger-ui.css"
            else:
                base = "https://unpkg.com/swagger-ui-dist@5.11.0"
                js_url = f"{base}/swagger-ui-bundle.js"
                css_url = f"{base}/swagger-ui.css"
            return get_swagger_ui_html(
                openapi_url=openapi_url,
                title=f"{settings.APP_NAME} - Swagger UI",
                swagger_js_url=js_url,
                swagger_css_url=css_url,
            )

        self.application.add_api_route(
            f"{settings.API_V1_PREFIX}/docs",
            swagger_ui_html,
            methods=["GET"],
            include_in_schema=False,
        )
        register_middleware(self.application)
        self.application.add_event_handler("startup", self.on_init_app())
        self.application.add_event_handler("shutdown", self.on_terminate_app())


app = App().application
