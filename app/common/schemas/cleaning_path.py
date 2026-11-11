from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CleaningPathPreviewRequest(BaseModel):
    """Identify the map-assigned robot whose path will be previewed."""

    robot_id: UUID


class CleaningPathPoint(BaseModel):
    """One coordinate in the map's local X/Y frame, measured in meters."""

    x: float
    y: float


class CleaningPathRingLayer(BaseModel):
    """One numbered ring with its coordinates in travel order."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^ring_layer_[1-9][0-9]*$")
    points: list[CleaningPathPoint]


class CleaningPathPreviewData(BaseModel):
    """Fixed response shape for rings, center travel, and dock return."""

    model_config = ConfigDict(extra="forbid")

    ring_layers: list[CleaningPathRingLayer]
    to_center: list[CleaningPathPoint]
    return_to_dock: list[CleaningPathPoint]
