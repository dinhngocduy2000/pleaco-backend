from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from app.common.context import AppContext

TRequest = TypeVar("TRequest")
TResponse = TypeVar("TResponse")


class BaseNotificationChannels(ABC, Generic[TRequest, TResponse]):
    """
    Abstract base class for all external service integrations.

    To add a new external service (e.g., SMS, push notification, payment):
      1. Define request/response schemas
      2. Create a subclass implementing `send()`
      3. Inject the subclass where needed via the App wiring in main.py
    """

    @abstractmethod
    def send(self, request: TRequest, ctx: AppContext) -> TResponse:
        pass

    @abstractmethod
    def _build_message(self, request: TRequest, ctx: AppContext) -> None:
        pass

    @abstractmethod
    def push(self, request: TRequest, ctx: AppContext) -> TResponse:
        pass
