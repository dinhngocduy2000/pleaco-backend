from enum import Enum


class RobotModel(str, Enum):
    STANDARD = "STANDARD"
    LITE = "LITE"
    PRO = "PRO"


class RobotConnectionStatus(str, Enum):
    ONLINE = "ONLINE"
    STALE = "STALE"
    OFFLINE = "OFFLINE"


class RobotOperationalStatus(str, Enum):
    IDLE = "IDLE"
    EXECUTING = "EXECUTING"
    CHARGING = "CHARGING"
