from enum import Enum


class EventPriority(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class EventCategory(str, Enum):
    HANG_OUT = "hang_out"
    DATE = "date"
    BUSINESS = "business"
    COFFEE_HOPPING = "coffee"
    FOOD_TOUR = "food"
    GAMING = "gaming"
    MOVIE = "movie"
    OTHER = "other"
