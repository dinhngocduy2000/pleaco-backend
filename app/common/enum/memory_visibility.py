from enum import Enum


class MemoryVisibility(str, Enum):
    PUBLIC = "public"
    GROUP = "group"
    PRIVATE = "private"
