import math

import pytest
from shapely.geometry import LineString, Point, box

from app.services.cleaning_path_planner import PlanningError, plan_cleaning_path


def flatten(sections):
    route = []
    for points in sections.values():
        route.extend(points if not route else points[1:])
    return route


def assert_continuous(route):
    assert route
    assert all(math.dist(a, b) <= 1.000001 for a, b in zip(route, route[1:]))


def test_complete_west_first_rings_and_dock_return():
    dock = box(.5, .5, 1.5, 1.5)
    sections = plan_cleaning_path(12, 12, (1, 1), [], own_dock=dock)
    route = flatten(sections)
    assert list(sections) == ["ring_layer_1", "ring_layer_2",
                              "ring_layer_3", "ring_layer_4", "ring_layer_5",
                              "to_center", "return_to_dock"]
    assert sections["ring_layer_1"][0] == (1, 1)
    assert all(section for section in sections.values())
    assert all(a[-1] == b[0] for a, b in zip(sections.values(), list(sections.values())[1:]))
    assert route[0] == route[-1] == (1, 1)
    assert (6, 6) in route
    assert_continuous(route)
    # The first traversed ring begins by heading west after dock egress.
    first = next(i for i, p in enumerate(route) if p[1] == 1 and p[0] > 2.5)
    assert route[first + 1][0] <= route[first][0] or route[first + 1][1] > route[first][1]


def test_obstacle_segments_are_clear():
    obstacle = box(4, 3, 5, 4)
    buffered = obstacle.buffer(1)
    route = flatten(plan_cleaning_path(12, 12, (2, 2), [obstacle]))
    assert route[0] == route[-1] == (2, 2)
    assert (6, 6) in route
    assert_continuous(route)
    assert all(not buffered.intersects(LineString([a, b])) for a, b in zip(route, route[1:]))


def test_unreachable_ring_is_rejected():
    with pytest.raises(PlanningError):
        plan_cleaning_path(12, 12, (2, 2), [box(0, 5, 12, 7)])


@pytest.mark.parametrize("start,dock", [
    ((0.5, 3), box(0, 2, 1, 4)),
    ((11.5, 3), box(11, 2, 12, 4)),
    ((3, 0.5), box(2, 0, 4, 1)),
    ((3, 11.5), box(2, 11, 4, 12)),
    ((3, 0), box(2, 0, 4, 1)),
])
def test_edge_dock_position_is_first_ring_point(start, dock):
    sections = plan_cleaning_path(12, 12, start, [], own_dock=dock)
    assert "to_first_ring" not in sections
    assert sections["ring_layer_1"][0] == start
    route = flatten(sections)
    assert route[0] == route[-1] == start
    assert (6, 6) in route
    assert_continuous(route)
