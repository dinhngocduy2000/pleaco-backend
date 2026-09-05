from enum import Enum


class MapStatus(str, Enum):
    ASSIGNED = "ASSIGNED"
    UNASSIGNED = "UNASSIGNED"


class MapBoundarySource(str, Enum):
    DIMENSIONS = "DIMENSIONS"
    CUSTOM = "CUSTOM"
    TEACH_MODE = "TEACH_MODE"
