"""Alternate left/right steering; each backend-timed leg ends at neutral."""
from boats.heron.experiments.motion_timed._common import (
    ZIGZAG_LEG_S, ZIGZAG_THROTTLE, ZIGZAG_STEERING,
)


def run(mc):
    with mc.armed_manual():
        with mc.control() as control:
            for direction in (-1, 1, -1, 1):
                control.pulse(
                    ZIGZAG_LEG_S,
                    throttle=ZIGZAG_THROTTLE,
                    steering=direction * ZIGZAG_STEERING,
                )


EXPERIMENTS = {"timed-zigzag": run}
