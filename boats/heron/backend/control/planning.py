"""Serializable route intent. No networking, persistence, or vehicle dependencies."""
from dataclasses import asdict, dataclass
import math


def number(value, low, high, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be a finite number in [{low}, {high}]")
    return float(value)


@dataclass(frozen=True)
class Waypoint:
    """Geographic point in decimal degrees: latitude [-85, 85], longitude [-180, 180]."""
    lat: float
    lon: float

    def __post_init__(self):
        object.__setattr__(self, "lat", number(self.lat, -85, 85, "latitude"))
        object.__setattr__(self, "lon", number(self.lon, -180, 180, "longitude"))

    @classmethod
    def from_dict(cls, value):
        """Construct from a mapping with numeric lat and lon in decimal degrees."""
        return cls(value["lat"], value["lon"])


@dataclass(frozen=True)
class LinearThrottleRamp:
    """Interpolate normalized throttle from start to end by distance along a leg.

    Endpoints are in [-1, 1]; Plans require nonnegative (forward) profiles.
    Linear is a compatibility alias. Values describe requested input, not speed.
    """

    start: float
    end: float

    def __post_init__(self):
        object.__setattr__(self, "start", number(self.start, -1, 1, "starting demand"))
        object.__setattr__(self, "end", number(self.end, -1, 1, "ending demand"))

    def evaluate(self, fraction):
        """Return throttle at a route fraction, clamping fraction to [0, 1]."""
        return self.start + (self.end - self.start) * max(0, min(1, fraction))


def ConstantThrottle(value):
    """Return a constant LinearThrottleRamp with normalized input value in [-1, 1].

    Plans accept forward inputs [0, 1] only. Constant is a compatibility alias.
    """
    return LinearThrottleRamp(value, value)


# Compatibility for existing scripts; new callers should use the explicit names.
Linear = LinearThrottleRamp
Constant = ConstantThrottle


@dataclass(frozen=True)
class Leg:
    """Direct route segment between distinct Waypoints with a throttle profile."""
    start: Waypoint
    end: Waypoint
    throttle: LinearThrottleRamp

    def __post_init__(self):
        if not isinstance(self.start, Waypoint) or not isinstance(self.end, Waypoint) or not isinstance(self.throttle, LinearThrottleRamp):
            raise ValueError("A leg needs two Waypoints and a throttle profile")
        if self.start == self.end:
            raise ValueError("A leg must have different endpoints")


@dataclass(frozen=True)
class Plan:
    """Continuous forward route of 1–256 Legs sharing consecutive endpoints.

    arrival_radius_m is the final endpoint tolerance (0.1–5 metres).
    launch_throttle is optional starting input assistance (0–1, default off).
    max_tracking_error_m limits permitted route deviation (0.1–20 metres).
    max_duration_s is the execution failure limit (1–86400 seconds).
    All leg throttle profiles must be nonnegative. Construction validates values;
    geographic clearance is checked separately by the backend.
    """
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
        """Return the route's ordered tuple of endpoints, including its start."""
        return (self.legs[0].start,) + tuple(leg.end for leg in self.legs)

    def to_dict(self):
        """Return the versioned, JSON-serializable plan sent to the backend."""
        return {"version": 1, **asdict(self)}

    @classmethod
    def from_dict(cls, value):
        """Parse a serialized plan; reject unsupported versions or invalid values."""
        if value.get("version", 1) != 1:
            raise ValueError("Unsupported plan version")
        return cls(
            legs=tuple(Leg(Waypoint.from_dict(leg["start"]), Waypoint.from_dict(leg["end"]),
                           LinearThrottleRamp(**leg["throttle"])) for leg in value["legs"]),
            **{key: value[key] for key in ("arrival_radius_m", "launch_throttle", "max_tracking_error_m", "max_duration_s") if key in value},
        )
