"""Increase thrust to the route midpoint, then decrease it to the endpoint."""

from backend.sdk import Leg, LinearThrottleRamp, Plan, Waypoint
from experiments._common import START, END, START_HEADING_DEG, TRANSIT_SPEED_MPS


def run(mc):
    mc.navigate(START, speed_mps=TRANSIT_SPEED_MPS, heading_deg=START_HEADING_DEG)
    midpoint = Waypoint((START.lat + END.lat) / 2, (START.lon + END.lon) / 2)
    plan = Plan(
        [
            Leg(START, midpoint, throttle=LinearThrottleRamp(start=0, end=1)),
            Leg(midpoint, END, throttle=LinearThrottleRamp(start=1, end=0)),
        ],
        launch_throttle=0.05,
        arrival_radius_m=1,
    )
    mc.run("acceleration-to-end", plan).wait_until_finished()
    mc.navigate(START, speed_mps=TRANSIT_SPEED_MPS, heading_deg=START_HEADING_DEG)
