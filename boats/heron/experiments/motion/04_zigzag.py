"""Alternate timed moves northeast and southeast; each move ends at neutral."""
from boats.heron.experiments.motion._common import START, END, START_HEADING_DEG, TRANSIT_SPEED_MPS


def run(mc):
    with mc.armed_manual():
        mc.navigate(START, speed_mps=TRANSIT_SPEED_MPS, heading_deg=START_HEADING_DEG)
        with mc.control(start=START, end=END, heading_deg=START_HEADING_DEG) as control:
            for heading_deg in (70, 110, 70, 110):
                control.move(speed_mps=0.6, heading_deg=heading_deg, duration_s=3)
        mc.navigate(END, speed_mps=TRANSIT_SPEED_MPS)
        mc.navigate(START, speed_mps=TRANSIT_SPEED_MPS, heading_deg=START_HEADING_DEG)
