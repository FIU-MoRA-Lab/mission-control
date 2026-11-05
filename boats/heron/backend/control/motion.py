"""Movement goals and feedback control, independent of transport and experiments."""
import math

from boats.heron.backend.mapping.constraints import xy
from .planning import Waypoint, number


def heading_error(target, actual):
    return (target - actual + 180) % 360 - 180


class Motion:
    """A finite movement, stepped by the backend clock using measured state.

    Speed uses a bounded PI controller; heading uses proportional steering.
    These initial gains require vehicle validation before relying on live tracking.
    Ground speed is unsigned, so movement goals currently support forward travel.
    """

    def __init__(self, operation, data, heading):
        self.operation = operation
        self.heading = heading
        self.integral = 0.0
        self.settled_s = 0.0
        self.destination = None
        self.turn_rate = None
        self.acceleration = None
        self.target_speed = 0.0
        if operation == "move":
            self.speed = number(data["speed_mps"], 0, 2, "speed_mps")
            self.duration = number(data["duration_s"], 0.05, 600, "duration_s")
            if data.get("acceleration_mps2") is not None:
                self.acceleration = number(data["acceleration_mps2"], 0.01, 2, "acceleration_mps2")
            if data.get("heading_deg") is not None and data.get("turn_rate_deg_s") is not None:
                raise ValueError("Choose heading_deg or turn_rate_deg_s, not both")
            if data.get("heading_deg") is not None:
                self.heading = number(data["heading_deg"], 0, 360, "heading_deg")
            if data.get("turn_rate_deg_s") is not None:
                self.turn_rate = number(data["turn_rate_deg_s"], -60, 60, "turn_rate_deg_s")
        elif operation == "turn_to":
            self.speed = 0.0
            self.heading = number(data["heading_deg"], 0, 360, "heading_deg")
            self.tolerance = number(data.get("tolerance_deg", 3), 0.5, 15, "tolerance_deg")
            self.duration = number(data.get("timeout_s", 60), 0.05, 600, "timeout_s")
        elif operation == "go_to":
            self.speed = number(data.get("speed_mps", 1), 0.1, 2, "speed_mps")
            self.destination = Waypoint.from_dict(data["destination"])
            self.tolerance = number(data.get("arrival_radius_m", 1), 0.1, 5, "arrival_radius_m")
            self.duration = number(data.get("timeout_s", 180), 0.05, 600, "timeout_s")
        else:
            raise ValueError("Unknown movement operation")

    def step(self, elapsed, dt, position, heading, speed):
        if self.operation == "move" and elapsed >= self.duration:
            return 0.0, 0.0, True
        if elapsed >= self.duration:
            raise ValueError(f"{self.operation} timed out before reaching its goal")
        desired_speed = self.speed
        if self.acceleration is not None:
            # Duration includes acceleration from rest and deceleration toward rest.
            desired_speed = min(desired_speed, self.acceleration * max(0, elapsed),
                                self.acceleration * max(0, self.duration - elapsed))
        if self.destination is not None:
            dx, dy = xy(self.destination, position)
            distance = math.hypot(dx, dy)
            if distance <= self.tolerance:
                return 0.0, 0.0, True
            self.heading = math.degrees(math.atan2(dx, dy)) % 360
            desired_speed = min(desired_speed, max(0.1, distance * 0.5))
        if self.turn_rate is not None:
            self.heading = (self.heading + self.turn_rate * dt) % 360
        error = heading_error(self.heading, heading)
        if self.operation == "turn_to":
            self.settled_s = self.settled_s + dt if abs(error) <= self.tolerance else 0.0
            if self.settled_s >= 0.3:
                return 0.0, 0.0, True
        # Reduce translation during large turns instead of driving away from a goal.
        desired_speed *= max(0.0, math.cos(math.radians(error)))
        self.target_speed = desired_speed
        steering = max(-0.5, min(0.5, error / 60))
        if desired_speed == 0:
            self.integral = 0.0
            return 0.0, steering, False
        speed_error = desired_speed - speed
        candidate = max(0.0, min(1.0, self.integral + 0.3 * speed_error * dt))
        demand = 0.4 * speed_error + candidate
        # Anti-windup: retain the integrator when it would push further into saturation.
        if 0 <= demand <= 1 or demand > 1 and speed_error < 0 or demand < 0 and speed_error > 0:
            self.integral = candidate
        throttle = max(0.0, min(1.0, 0.4 * speed_error + self.integral))
        return throttle, steering, False
