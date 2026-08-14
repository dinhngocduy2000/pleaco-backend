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
from app.external.redis.redis import RedisClient
from app.handler.auth import AuthHandler
from app.handler.group import GroupHandler
from app.handler.mail import MailHandler
from app.handler.user import UserHandler
from app.repository.registry import Registry
from app.router.auth import AuthRouter
from app.router.group import GroupRouter
from app.router.mail import MailRouter
from app.router.user import UserRouter
from app.services.auth import AuthService
from app.services.group import GroupService
from app.services.user import UserService


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
            await init_topics(
                consumers={"verification-email": verification_topic.start_consumer}
            )

            # ------------ Service ------------
            user_service = UserService(repo=registry)
            auth_service = AuthService(
                repo=registry,
                user_service=user_service,
                mail_service=mail_service,
                verification_topic=verification_topic,
            )
            permission_service = PermissionService(repo=registry)
            group_service = GroupService(repo=registry, permission_service=permission_service)
            AuthMiddleware.init(auth_service=auth_service)

            # ------------ Handler ------------
            user_handler = UserHandler(service=user_service)
            auth_handler = AuthHandler(service=auth_service)
            mail_handler = MailHandler(service=mail_service)
            group_handler = GroupHandler(service=group_service)

            # ------------ Router ------------
            user_router = UserRouter(handler=user_handler)
            auth_router = AuthRouter(handler=auth_handler)
            mail_router = MailRouter(handler=mail_handler)
            group_router = GroupRouter(handler=group_handler)
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

        return start_app

    def on_terminate_app(self) -> Callable:
        @logger.catch
        async def stop_app() -> None:
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
