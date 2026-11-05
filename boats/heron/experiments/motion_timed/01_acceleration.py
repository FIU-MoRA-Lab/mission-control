"""Smoothly raise forward input to 100%, then lower it to neutral by time."""
from boats.heron.experiments.motion_timed._common import RAMP_UP_S, RAMP_DOWN_S


def run(mc):
    with mc.armed_manual():
        with mc.control() as control:
            control.play([
                (0, {"throttle": 0, "steering": 0}),
                (RAMP_UP_S, {"throttle": 1, "steering": 0}),
                (RAMP_UP_S + RAMP_DOWN_S, {"throttle": 0, "steering": 0}),
            ])


EXPERIMENTS = {"timed-acceleration": run}
