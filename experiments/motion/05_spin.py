"""Turn toward a new compass heading without requesting forward motion."""
from experiments.motion._common import START, END, START_HEADING_DEG, TRANSIT_SPEED_MPS


def run(mc):
    with mc.armed_manual():
        mc.navigate(START, speed_mps=TRANSIT_SPEED_MPS, heading_deg=START_HEADING_DEG)
        with mc.control(start=START, end=END, heading_deg=START_HEADING_DEG) as control:
            control.turn_to(heading_deg=270, timeout_s=20)
        mc.navigate(START, speed_mps=TRANSIT_SPEED_MPS, heading_deg=START_HEADING_DEG)
