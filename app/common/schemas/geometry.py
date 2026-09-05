from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.common.enum.geometry import GeometryType


Coordinate = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Position = tuple[Coordinate, Coordinate]
LinearRing = Annotated[list[Position], Field(min_length=4)]


class PolygonGeometry(BaseModel):
    """Polygon-shaped JSON using local map X/Y coordinates, including holes."""

    model_config = ConfigDict(extra="forbid")

    type: Literal[GeometryType.POLYGON]
    coordinates: list[LinearRing] = Field(min_length=1)

    @field_validator("coordinates")
    @classmethod
    def rings_must_be_closed(cls, rings: list[list[Position]]) -> list[list[Position]]:
        if any(ring[0] != ring[-1] for ring in rings):
            raise ValueError("Polygon rings must be explicitly closed")
        return rings
