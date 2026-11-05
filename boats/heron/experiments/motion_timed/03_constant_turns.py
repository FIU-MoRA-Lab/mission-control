"""Timed clockwise steering inputs; angular rate is not controlled."""
from functools import partial
from boats.heron.experiments.motion_timed._common import TURN_S, TURN_THROTTLE, TURN_STEERING


def turn(mc, steering):
    with mc.armed_manual():
        with mc.control() as control:
            control.pulse(TURN_S, throttle=TURN_THROTTLE, steering=steering)


EXPERIMENTS = {
    f"timed-turn-steering-{round(steering * 100)}": partial(turn, steering=steering)
    for steering in TURN_STEERING
}
