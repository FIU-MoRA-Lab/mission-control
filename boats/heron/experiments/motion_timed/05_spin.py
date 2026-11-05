"""Request clockwise steering and zero forward input for a fixed duration."""
from boats.heron.experiments.motion_timed._common import SPIN_S, SPIN_STEERING


def run(mc):
    with mc.armed_manual():
        with mc.control() as control:
            control.pulse(SPIN_S, throttle=0, steering=SPIN_STEERING)


EXPERIMENTS = {"timed-spin": run}
