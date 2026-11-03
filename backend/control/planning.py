"""Serializable route intent. No networking, persistence, or vehicle dependencies."""
from dataclasses import asdict, dataclass
import math


def number(value, low, high, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be a finite number in [{low}, {high}]")
    return float(value)


@dataclass(frozen=True)
class Waypoint:
    lat: float
    lon: float

    def __post_init__(self):
        object.__setattr__(self, "lat", number(self.lat, -85, 85, "latitude"))
        object.__setattr__(self, "lon", number(self.lon, -180, 180, "longitude"))

    @classmethod
    def from_dict(cls, value):
        return cls(value["lat"], value["lon"])


@dataclass(frozen=True)
class Linear:
    start: float
    end: float

    def __post_init__(self):
        object.__setattr__(self, "start", number(self.start, -1, 1, "starting demand"))
        object.__setattr__(self, "end", number(self.end, -1, 1, "ending demand"))

    def evaluate(self, fraction):
        return self.start + (self.end - self.start) * max(0, min(1, fraction))


def Constant(value):
    return Linear(value, value)


@dataclass(frozen=True)
class Leg:
    start: Waypoint
    end: Waypoint
    throttle: Linear

    def __post_init__(self):
        if not isinstance(self.start, Waypoint) or not isinstance(self.end, Waypoint) or not isinstance(self.throttle, Linear):
            raise ValueError("A leg needs two Waypoints and a throttle profile")
        if self.start == self.end:
            raise ValueError("A leg must have different endpoints")


@dataclass(frozen=True)
class Plan:
    legs: tuple[Leg, ...]
    arrival_radius_m: float = 1.0
    launch_throttle: float = 0.0
    max_tracking_error_m: float = 3.0
    max_duration_s: float = 600.0

    def __post_init__(self):
        object.__setattr__(self, "legs", tuple(self.legs))
        if not 1 <= len(self.legs) <= 256 or not all(isinstance(leg, Leg) for leg in self.legs):
            raise ValueError("A plan needs 1–256 legs")
        for before, after in zip(self.legs, self.legs[1:]):
            if before.end != after.start:
                raise ValueError("Consecutive legs must share an endpoint")
        number(self.arrival_radius_m, 0.1, 5, "arrival radius")
        number(self.launch_throttle, 0, 1, "launch throttle")
        number(self.max_tracking_error_m, 0.1, 20, "maximum tracking error")
        number(self.max_duration_s, 1, 86400, "maximum run duration")
        if any(min(leg.throttle.start, leg.throttle.end) < 0 for leg in self.legs):
            raise ValueError("Route profiles use forward throttle; use direct controls for reverse thrust")

    @property
    def waypoints(self):
        return (self.legs[0].start,) + tuple(leg.end for leg in self.legs)

    def to_dict(self):
        return {"version": 1, **asdict(self)}

    @classmethod
    def from_dict(cls, value):
        if value.get("version", 1) != 1:
            raise ValueError("Unsupported plan version")
        return cls(
            legs=tuple(Leg(Waypoint.from_dict(leg["start"]), Waypoint.from_dict(leg["end"]),
                           Linear(**leg["throttle"])) for leg in value["legs"]),
            **{key: value[key] for key in ("arrival_radius_m", "launch_throttle", "max_tracking_error_m", "max_duration_s") if key in value},
        )
