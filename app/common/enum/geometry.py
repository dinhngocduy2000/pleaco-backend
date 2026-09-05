from enum import Enum


class GeometryType(str, Enum):
    POLYGON = "Polygon"
    POINT = "Point"
    LINE_STRING = "LineString"
