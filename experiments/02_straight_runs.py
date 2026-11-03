"""Steady equal-throttle inputs; measured speed is a result, not a setpoint."""
from functools import partial
from backend.sdk import Plan, Leg, Constant
from experiments._common import HOME, END, thrust, steady


def straight(mc, level):
    if getattr(mc,"testing_without_gps",False):
        with thrust(mc) as control:
            steady(control,5,level,level)
        return
    plan = Plan([Leg(HOME,END,Constant(level))],arrival_radius_m=1)
    mc.run(f"straight-{round(level*100)}-to-east",plan).wait_until_finished()


EXPERIMENTS = {
    "straight-20": partial(straight, level=.2),
    "straight-50": partial(straight, level=.5),
    "straight-80": partial(straight, level=.8),
}
