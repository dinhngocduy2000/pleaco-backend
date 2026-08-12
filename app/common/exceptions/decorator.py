from functools import wraps

from pydantic import ValidationError

from app.common.exceptions import (
    BadRequestException,
    ExceptionInternalError,
    ForbiddenException,
    UnauthorizedException,
)
from app.common.middleware.logger import Logger

logger = Logger()


def exception_handler(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except BadRequestException as e:
            logger.error(f"HTTP exception: {e}")
            raise e
        except UnauthorizedException as e:
            logger.error(f"HTTP exception: {e}")
            raise e
        except ForbiddenException as e:
            logger.error(f"HTTP Fobidden exception: {e}")
            raise e
        except ValidationError as e:
            logger.error(f"Validation error: {e}")
            # Extract first error message
            error_msg = e.errors()[0]["msg"] if e.errors() else "Validation failed"
            raise BadRequestException(message=error_msg)
        except Exception as e:
            logger.error(f"Internal server error: {e}")
            raise ExceptionInternalError from e

    return wrapper
