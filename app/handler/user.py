from app.common.exceptions import BadRequestException
from app.common.exceptions.decorator import exception_handler
from app.services.user import UserService
from app.common.schemas.user import UserCreate, UserInfo, UserLogin, UserLoginResponse


class UserHandler:
    service: UserService

    def __init__(self, service: UserService) -> None:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    @exception_handler
    async def create_user(self, user_data: UserCreate) -> UserInfo:
        raise NotImplementedError("Pleaco-specific implementation is pending.")

    @exception_handler
    async def login_user(self, login_request: UserLogin) -> UserLoginResponse:
        raise NotImplementedError("Pleaco-specific implementation is pending.")
