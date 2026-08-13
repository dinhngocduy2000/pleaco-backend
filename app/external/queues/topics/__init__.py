"""Topic abstractions for domain services."""

from app.external.queues.topics.base import Topic
from app.external.queues.topics.user_verification import UserVerificationTopic

__all__ = ["Topic", "UserVerificationTopic"]
