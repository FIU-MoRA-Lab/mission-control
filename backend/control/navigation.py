"""Pure route tracking and steering. No clock, threads, I/O, or MAVLink."""
from dataclasses import dataclass
import math
from backend.mapping.constraints import xy, projection


@dataclass(frozen=True)
class Guidance:
    leg: int
    progress: float
    tracking_error_m: float
    requested_throttle: float
    throttle: float
    steering: float
    completed: bool = False


class RouteFollower:
    def __init__(self, plan):
        self.plan = plan
        self.leg = 0
        self.launched = False

    def step(self, position, heading_deg):
        plan = self.plan
        # Only advance through successive endpoints; never select a nearest leg.
        while True:
            leg = plan.legs[self.leg]
            p, end = xy(position, leg.start), xy(leg.end, leg.start)
            progress, error = projection(p, (0,0), end)
            if math.dist(p,end) > plan.arrival_radius_m:
                break
            if self.leg == len(plan.legs)-1:
                return Guidance(self.leg, 1, error, 0, 0, 0, True)
            self.leg += 1
        length = math.hypot(*end)
        lookahead = min(1, progress + 2.0 / length)
        target = end[0]*lookahead-p[0], end[1]*lookahead-p[1]
        desired = math.degrees(math.atan2(target[0],target[1]))
        heading_error = (desired-heading_deg+180) % 360 - 180
        steering = max(-1, min(1, heading_error / 60.0))
        requested = leg.throttle.evaluate(progress)
        if requested >= plan.launch_throttle:
            self.launched = True
        throttle = max(requested,plan.launch_throttle) if not self.launched and self.leg == 0 else requested
        return Guidance(self.leg, progress, error, requested, throttle, steering)
