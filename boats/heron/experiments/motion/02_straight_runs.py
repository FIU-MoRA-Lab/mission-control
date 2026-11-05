"""Travel from start to end at a fixed normalized thrust input."""
from functools import partial
from boats.heron.backend.sdk import ConstantThrottle, Leg, Plan
from boats.heron.experiments.motion._common import START, END, START_HEADING_DEG, TRANSIT_SPEED_MPS


def straight(mc, thrust):
    with mc.armed_manual():
        mc.navigate(START, speed_mps=TRANSIT_SPEED_MPS, heading_deg=START_HEADING_DEG)
        plan = Plan([Leg(START, END, throttle=ConstantThrottle(thrust))], arrival_radius_m=1)
        mc.run(f"straight-{round(thrust * 100)}", plan).wait_until_finished()
        mc.navigate(START, speed_mps=TRANSIT_SPEED_MPS, heading_deg=START_HEADING_DEG)

EXPERIMENTS = {
    "straight-25": partial(straight, thrust=0.25),
    "straight-50": partial(straight, thrust=0.50),
    "straight-75": partial(straight, thrust=0.75),
    "straight-100": partial(straight, thrust=1.00),
}
