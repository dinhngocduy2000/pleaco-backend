from enum import Enum


class EnvironmentZoneType(str, Enum):
    NO_GO = "NO_GO"
    OBSTACLE = "OBSTACLE"
    CLEANING_ZONE = "CLEANING_ZONE"
