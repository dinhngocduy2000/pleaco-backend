from fastapi import HTTPException, status


class BaseException(HTTPException):
    def __init__(self, message: str, status_code: int):
        self.message = message
        self.status_code = status_code
        super().__init__(status_code=status_code, detail=message)


class ExceptionInternalError(HTTPException):
    def __init__(self):
        super().__init__(status_code=500, detail="Internal server error")


class BadRequestException(BaseException):
    def __init__(self, message: str, status_code: int = status.HTTP_400_BAD_REQUEST):
        super().__init__(status_code=status_code, message=message)


class UnauthorizedException(BaseException):
    def __init__(self, message: str, status_code: int = status.HTTP_401_UNAUTHORIZED):
        super().__init__(status_code=status_code, message=message)


class ForbiddenException(BaseException):
    def __init__(self, message: str, status_code: int = status.HTTP_403_FORBIDDEN):
        super().__init__(status_code=status_code, message=message)


class NotFoundException(BaseException):
    def __init__(self, message: str, status_code: int = status.HTTP_404_NOT_FOUND):
        super().__init__(status_code=status_code, message=message)
