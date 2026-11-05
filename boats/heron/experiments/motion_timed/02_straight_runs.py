"""Request equal forward input for a fixed duration, without heading feedback."""
from functools import partial
from boats.heron.experiments.motion_timed._common import STRAIGHT_S


def straight(mc, thrust):
    with mc.armed_manual():
        with mc.control() as control:
            control.pulse(STRAIGHT_S, throttle=thrust, steering=0)


EXPERIMENTS = {
    "timed-straight-25": partial(straight, thrust=0.25),
    "timed-straight-50": partial(straight, thrust=0.50),
    "timed-straight-75": partial(straight, thrust=0.75),
    "timed-straight-100": partial(straight, thrust=1.00),
}
