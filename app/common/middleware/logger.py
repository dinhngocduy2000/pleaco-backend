import logging

from app.common.context import AppContext

formatter = "%(asctime)s - %(levelname)s - %(pathname)s, line: %(lineno)d - %(message)s"


class Logger:
    app_name: str = "pleaco-backend"

    def __init__(self) -> None:
        self.logger = logging.getLogger(self.app_name)
        self.logger.setLevel(logging.DEBUG)

        if not self.logger.handlers:
            log_handler = logging.StreamHandler()
            log_handler.setLevel(logging.DEBUG)
            log_handler.setFormatter(logging.Formatter(formatter))
            self.logger.addHandler(log_handler)

    def __customize_message(self, msg, **kwargs):
        context = kwargs.get("context")
        if context is not None and isinstance(context, AppContext):
            return f"{context.log_metadata()} - message: {msg}"
        return f"message: {msg}"

    def debug(self, msg, *args, **kwargs):
        message = self.__customize_message(msg, **kwargs)
        self.logger.debug(message, *args, stacklevel=2)

    def warning(self, msg, *args, **kwargs):
        message = self.__customize_message(msg, **kwargs)
        self.logger.warning(message, *args, stacklevel=2)

    def error(self, msg, *args, **kwargs):
        message = self.__customize_message(msg, **kwargs)
        self.logger.error(message, *args, stacklevel=2)

    def info(self, msg, *args, **kwargs):
        message = self.__customize_message(msg, **kwargs)
        self.logger.info(message, *args, stacklevel=2)

    def exception(self, msg, *args, **kwargs):
        message = self.__customize_message(msg, **kwargs)
        self.logger.exception(message, *args, stacklevel=2)
