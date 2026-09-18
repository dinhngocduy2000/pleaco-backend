"""Validated payloads and connection state for Socket.IO realtime events."""

from pydantic import BaseModel, ConfigDict, Field
from uuid import UUID

from app.common.schemas.user import Credential


class MapSubscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    map_id: UUID = Field(alias="mapId")


class SocketSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential: Credential
