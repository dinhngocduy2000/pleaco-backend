"""Bounded rectangular cleaning path planning with orthogonal obstacle detours."""

from __future__ import annotations

import heapq
import math
from decimal import Decimal
from typing import Iterator, Literal, Protocol

from shapely.geometry import LineString, Point, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from app.common.context import AppContext
from app.common.enum.direction import Direction
from app.common.middleware.logger import Logger
from app.common.utils.robot_movement import RectangleBoundary, select_movement

logger = Logger()

Point2D = tuple[float, float]
RingSide = Literal["south", "west", "north", "east"]
# Bound route size and graph search so a preview cannot consume unbounded work.
MAX_POINTS = 20000
MAX_NODES = 40000
EPSILON = 1e-6


class PlanningError(ValueError):
    """The required complete route cannot be safely generated."""


class InsetRectangle:
    """Represent the travel area after applying an edge inset."""

    def __init__(self, width: float, height: float, clearance: float) -> None:
        self.geometry = box(clearance, clearance, width - clearance, height - clearance)

    def covers(self, segment: LineString) -> bool:
        return self.geometry.covers(segment)


class _PrescribedStepBoundary:
    """Allow only one planned step when checking movement inside the map.

    select_movement may try other directions when its preferred move is blocked
    or the robot is at a map edge. A cleaning ring must follow its chosen step,
    so this boundary rejects every candidate endpoint except that step's target.
    """

    def __init__(
        self,
        map_boundary: RectangleBoundary,
        origin: tuple[Decimal, Decimal],
        target: tuple[Decimal, Decimal],
    ) -> None:
        self.map_boundary = map_boundary
        self.origin = origin
        self.target = target

    def contains(self, x: Decimal, y: Decimal) -> bool:
        # (x, y) is one point: either where this step starts (origin) or where
        # it ends (target). Both points must also lie inside the real map.
        return (x, y) in (self.origin, self.target) and self.map_boundary.contains(x, y)


def _points_are_close(a: Point2D, b: Point2D) -> bool:
    """Treat points less than EPSILON apart as the same position.

    Calculated coordinates can differ by a tiny amount even when they describe
    the same place on the route.
    """
    return math.dist(a, b) < EPSILON


def _steps(a: Point2D, b: Point2D) -> list[Point2D]:
    """Split one segment into points no farther than one meter apart."""
    length = math.dist(a, b)
    count = max(1, math.ceil(length - EPSILON))
    if count > MAX_POINTS:
        raise PlanningError("Cleaning path exceeds maximum response size")
    return [
        (a[0] + (b[0] - a[0]) * i / count, a[1] + (b[1] - a[1]) * i / count)
        for i in range(1, count + 1)
    ]


def _starting_side(
    width: float, height: float, start: Point2D
) -> tuple[RingSide, float]:
    """Use the nearest map edge to choose the dock's first ring orientation."""
    distances = (start[1], start[0], height - start[1], width - start[0])
    sides: tuple[RingSide, ...] = ("south", "west", "north", "east")
    return sides[distances.index(min(distances))], min(distances)


def _ring_corners(
    width: float, height: float, offset: float, start: Point2D, side: RingSide
) -> tuple[Point2D, list[Point2D]]:
    """Return the layer anchor and its ordered perimeter corners."""
    left, right = offset, width - offset
    south, north = offset, height - offset
    x = min(max(start[0], left), right)
    y = min(max(start[1], south), north)
    anchor = {
        "south": (x, south),
        "west": (left, y),
        "north": (x, north),
        "east": (right, y),
    }[side]
    corners = {
        "south": [(left, south), (left, north), (right, north), (right, south), anchor],
        "west": [(left, north), (right, north), (right, south), (left, south), anchor],
        "north": [(left, north), (left, south), (right, south), (right, north), anchor],
        "east": [(right, south), (left, south), (left, north), (right, north), anchor],
    }[side]
    return anchor, corners


def _cardinal_steps(
    origin: Point2D,
    target: Point2D,
    boundary: RectangleBoundary,
    width: float,
    height: float,
) -> list[Point2D]:
    """Validate the prescribed cardinal movement at each meter step."""
    if abs(target[0] - origin[0]) > EPSILON:
        direction = Direction.WEST if target[0] < origin[0] else Direction.EAST
    else:
        direction = Direction.NORTH if target[1] > origin[1] else Direction.SOUTH
    validated: list[Point2D] = []
    current_point = origin
    for step in _steps(origin, target):
        current = (Decimal(str(current_point[0])), Decimal(str(current_point[1])))
        destination = (Decimal(str(step[0])), Decimal(str(step[1])))
        distance = abs(destination[0] - current[0]) + abs(destination[1] - current[1])
        # For example, at the bottom edge select_movement tries north first.
        # If this ring step goes west, reject north and accept only its westward
        # destination, provided that destination remains inside the map.
        movement = select_movement(
            _PrescribedStepBoundary(boundary, current, destination),
            *current,
            distance,
            direction,
            width=Decimal(str(width)),
            height=Decimal(str(height)),
        )
        if movement is None or movement[0] != direction:
            raise PlanningError("Required ring step is outside map boundaries")
        validated.append(step)
        current_point = step
    return validated


def _ring_points(
    width: float, height: float, start: Point2D
) -> tuple[list[Point2D], list[int]]:
    """Build ordered perimeter waypoints and mark each ring's final index."""
    # 1. The dock position is the first ring point; keep its starting side.
    side, offset = _starting_side(width, height, start)
    points: list[Point2D] = [start]
    layer_ends: list[int] = []
    boundary = RectangleBoundary(Decimal(str(width)), Decimal(str(height)))
    while width - 2 * offset > EPSILON and height - 2 * offset > EPSILON:
        # 2. Move each rectangle one meter inward and traverse its perimeter.
        anchor, corners = _ring_corners(width, height, offset, start, side)
        if not _points_are_close(points[-1], anchor):
            points.extend(_steps(points[-1], anchor))
        for target in corners:
            if _points_are_close(points[-1], target):
                continue
            points.extend(_cardinal_steps(points[-1], target, boundary, width, height))
        # 3. Record this layer's end for response grouping.
        layer_ends.append(len(points) - 1)
        offset += 1.0
        if width - 2 * offset <= EPSILON or height - 2 * offset <= EPSILON:
            break
        if len(points) > MAX_POINTS:
            raise PlanningError("Cleaning path exceeds maximum response size")
    # 4. Append the center transition after the innermost ring.
    center = (width / 2, height / 2)
    if not _points_are_close(points[-1], center):
        points.extend(_steps(points[-1], center))
    return points, layer_ends


class _Graph:
    """Find safe orthogonal detours through ring and exclusion coordinates."""

    def __init__(
        self,
        width: float,
        height: float,
        clearance: float,
        obstacles: BaseGeometry,
        anchors: list[Point2D],
        *,
        ingress: bool = False,
    ) -> None:
        self.boundary = InsetRectangle(width, height, 0 if ingress else clearance)
        self.obstacles = obstacles
        self.xs, self.ys = self._grid_axes(width, height, clearance, obstacles, anchors)
        if len(self.xs) * len(self.ys) > MAX_NODES:
            raise PlanningError("Cleaning layout exceeds planning work limit")
        self.nx, self.ny = len(self.xs), len(self.ys)
        self.xindex = {x: i for i, x in enumerate(self.xs)}
        self.yindex = {y: i for i, y in enumerate(self.ys)}
        self.work = 0

    @staticmethod
    def _grid_axes(
        width: float,
        height: float,
        clearance: float,
        obstacles: BaseGeometry,
        anchors: list[Point2D],
    ) -> tuple[list[float], list[float]]:
        """Include anchors and lines just outside buffered obstacle bounds."""
        xs = {clearance, width - clearance, width / 2}
        ys = {clearance, height - clearance, height / 2}
        for x, y in anchors:
            xs.add(x)
            ys.add(y)
        for shape in getattr(obstacles, "geoms", [obstacles]):
            if shape.is_empty:
                continue
            min_x, min_y, max_x, max_y = shape.bounds
            for value in (min_x - 0.01, max_x + 0.01):
                if clearance <= value <= width - clearance:
                    xs.add(value)
            for value in (min_y - 0.01, max_y + 0.01):
                if clearance <= value <= height - clearance:
                    ys.add(value)
        return sorted(xs), sorted(ys)

    def _neighbors(self, node: tuple[int, int]) -> Iterator[tuple[int, int]]:
        """Yield in-bounds orthogonal neighbors of one grid node."""
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nxt = (node[0] + dx, node[1] + dy)
            if 0 <= nxt[0] < self.nx and 0 <= nxt[1] < self.ny:
                yield nxt

    def _point(self, node: tuple[int, int]) -> Point2D:
        return self.xs[node[0]], self.ys[node[1]]

    def _reconstruct(
        self,
        source: tuple[int, int],
        goal: tuple[int, int],
        previous: dict[tuple[int, int], tuple[int, int]],
    ) -> list[Point2D]:
        """Expand the chosen grid edges into points at most one meter apart."""
        nodes = [goal]
        while nodes[-1] != source:
            nodes.append(previous[nodes[-1]])
        nodes.reverse()
        result: list[Point2D] = []
        for u, v in zip(nodes, nodes[1:]):
            result.extend(_steps(self._point(u), self._point(v)))
        return result

    def safe(self, a: Point2D, b: Point2D) -> bool:
        """Check the entire segment, not only its endpoints, with Shapely."""
        line = LineString([a, b])
        return self.boundary.covers(line) and not self.obstacles.intersects(line)

    def route(self, start: Point2D, end: Point2D) -> list[Point2D]:
        """Return a direct safe segment or a bounded A* orthogonal detour."""
        if _points_are_close(start, end):
            return []
        # 2. Prefer the prescribed line when its full segment is traversable.
        aligned = abs(start[0] - end[0]) < EPSILON or abs(start[1] - end[1]) < EPSILON
        if aligned and self.safe(start, end):
            return _steps(start, end)
        source = (self.xindex[start[0]], self.yindex[start[1]])
        goal = (self.xindex[end[0]], self.yindex[end[1]])
        queue = [(math.dist(start, end), 0.0, source)]
        costs = {source: 0.0}
        previous: dict[tuple[int, int], tuple[int, int]] = {}
        # 3. Search neighboring grid nodes by estimated remaining distance.
        # The shared work counter caps all detours on this graph.
        while queue:
            _, cost, node = heapq.heappop(queue)
            if cost > costs[node] + EPSILON:
                continue
            if node == goal:
                break
            self.work += 1
            if self.work > MAX_NODES:
                raise PlanningError("Cleaning path exceeds planning work limit")
            for nxt in self._neighbors(node):
                a, b = self._point(node), self._point(nxt)
                if not self.safe(a, b):
                    continue
                candidate = cost + math.dist(a, b)
                if candidate + EPSILON < costs.get(nxt, math.inf):
                    costs[nxt] = candidate
                    previous[nxt] = node
                    heapq.heappush(
                        queue, (candidate + math.dist(b, end), candidate, nxt)
                    )
        if goal not in costs:
            raise PlanningError("Cannot reach required ring, center, or dock")
        # 4. Reconstruct the found route and split its edges into meter steps.
        return self._reconstruct(source, goal, previous)


class _RouteSections:
    """Keep ordered response sections continuous and within the point limit."""

    def __init__(self, start: Point2D) -> None:
        self.points = [start]
        self.sections: dict[str, list[Point2D]] = {"ring_layer_1": [start]}
        self.current_name = "ring_layer_1"

    @property
    def current(self) -> Point2D:
        return self.points[-1]

    def begin(self, name: str) -> None:
        """Repeat the previous endpoint so each section stands alone."""
        self.current_name = name
        self.sections[name] = [self.current]

    def append(self, segment: list[Point2D]) -> None:
        self.sections[self.current_name].extend(segment)
        self.points.extend(segment)
        if len(self.points) > MAX_POINTS:
            raise PlanningError("Cleaning path exceeds maximum response size")


def _validate_request(
    width: float, height: float, start: Point2D, clearance: float
) -> None:
    """Reject maps that cannot support a bounded rectangular preview."""
    if (
        not all(math.isfinite(v) for v in (*start, width, height, clearance))
        or clearance <= 0
        or min(width, height) <= 2 * clearance
    ):
        raise PlanningError(
            "Map dimensions or clearance cannot support a cleaning ring"
        )
    if width * height > MAX_POINTS / 4:
        raise PlanningError("Cleaning layout exceeds planning work limit")
    if not box(0, 0, width, height).covers(Point(start)):
        raise PlanningError("Robot position is outside map boundaries")


def plan_cleaning_path(
    width: float,
    height: float,
    start: Point2D,
    exclusions: list[BaseGeometry],
    clearance: float = 1.0,
    own_dock: BaseGeometry | None = None,
    ctx: AppContext | None = None,
) -> dict[str, list[Point2D]]:
    """Return ordered ring sections beginning at the robot's dock position."""
    # 1. Reject invalid or excessive maps before allocating rings or graph nodes.
    _validate_request(width, height, start, clearance)
    # 2. Buffer all exclusions by clearance. The robot's own dock is excluded
    # during ring travel but permitted while departing and returning.
    obstacles = unary_union(
        [shape.buffer(clearance) for shape in exclusions if not shape.is_empty]
    )
    dock_obstacle = own_dock.buffer(clearance) if own_dock is not None else None
    ring_obstacles = (
        unary_union([obstacles, dock_obstacle])
        if dock_obstacle is not None
        else obstacles
    )
    # 3. Build ring waypoints from the exact robot position, then create a
    # full-map graph for dock travel and a dock-avoiding graph for the rings.
    rings, layer_ends = _ring_points(width, height, start)
    first_offset = min(start[0], width - start[0], start[1], height - start[1])
    anchors = [start, *rings]
    transit = _Graph(width, height, clearance, obstacles, anchors, ingress=True)
    ring_graph = _Graph(width, height, first_offset, ring_obstacles, anchors)
    # 4. Start the first response layer at the robot position. Each later
    # section repeats its predecessor's last point to make sections standalone.
    route = _RouteSections(start)
    layer_number = 1
    departed_dock = dock_obstacle is None or not dock_obstacle.intersects(Point(start))
    for index, target in enumerate(rings[1:], start=1):
        while layer_number <= len(layer_ends) and index > layer_ends[layer_number - 1]:
            layer_number += 1
            route.begin(
                f"ring_layer_{layer_number}"
                if layer_number <= len(layer_ends)
                else "to_center"
            )
        # 5. Skip blocked prescribed waypoints and rejoin a later point through
        # an orthogonal detour. The first departure may pass through its dock.
        if obstacles.intersects(Point(target)):
            continue
        if not departed_dock:
            segment = transit.route(route.current, target)
            departed_dock = not dock_obstacle.intersects(Point(target))
        else:
            if ring_obstacles.intersects(Point(target)):
                continue
            segment = ring_graph.route(route.current, target)
        route.append(segment)
    # 6. Keep center travel and dock return separate from the named rings.
    if "to_center" not in route.sections:
        route.begin("to_center")
    route.begin("return_to_dock")
    route.append(transit.route(route.current, start))
    logger.debug(
        msg=f"Completed rectangular cleaning path with {len(route.points)} points",
        context=ctx,
    )
    return route.sections
