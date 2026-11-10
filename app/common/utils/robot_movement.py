"""Choose a full movement step inside an interchangeable map boundary."""

from decimal import Decimal
from typing import Protocol

from app.common.enum.direction import Direction


class MovementBoundary(Protocol):
    def contains(self, x: Decimal, y: Decimal) -> bool: ...


class RectangleBoundary:
    def __init__(self, width: Decimal, height: Decimal) -> None:
        self.width = width
        self.height = height

    def contains(self, x: Decimal, y: Decimal) -> bool:
        return x.is_finite() and y.is_finite() and 0 <= x <= self.width and 0 <= y <= self.height


STEPS: dict[Direction, tuple[int, int]] = {
    Direction.NORTH: (0, 1), Direction.SOUTH: (0, -1),
    Direction.EAST: (1, 0), Direction.WEST: (-1, 0),
}


def select_movement(
    boundary: MovementBoundary, x: Decimal, y: Decimal, distance: Decimal,
    direction: Direction, *, width: Decimal, height: Decimal,
) -> tuple[Direction, Decimal, Decimal] | None:
    """Return the preferred valid full step, or None when no direction fits."""
    if not boundary.contains(x, y):
        return None
    if y == height:
        preference = [Direction.SOUTH, Direction.NORTH, Direction.EAST, Direction.WEST]
    elif y == 0:
        preference = [Direction.NORTH, Direction.SOUTH, Direction.EAST, Direction.WEST]
    elif x == 0 or x == width:
        preference = [Direction.NORTH, Direction.SOUTH, Direction.EAST, Direction.WEST]
    else:
        preference = [direction, Direction.NORTH, Direction.SOUTH, Direction.EAST, Direction.WEST]
    for candidate in dict.fromkeys(preference):
        dx, dy = STEPS[candidate]
        target_x, target_y = x + dx * distance, y + dy * distance
        if boundary.contains(target_x, target_y):
            return candidate, target_x, target_y
    return None
