"""Maintain 0.6 m/s while turning clockwise at three angular rates."""
from functools import partial
from experiments._common import START, END, START_HEADING_DEG, TRANSIT_SPEED_MPS


def turn(mc, turn_rate_deg_s):
    mc.navigate(START, speed_mps=TRANSIT_SPEED_MPS, heading_deg=START_HEADING_DEG)
    with mc.control(start=START, end=END, heading_deg=START_HEADING_DEG) as control:
        control.move(speed_mps=0.6, turn_rate_deg_s=turn_rate_deg_s, duration_s=5)
    mc.navigate(START, speed_mps=TRANSIT_SPEED_MPS, heading_deg=START_HEADING_DEG)


EXPERIMENTS = {
    "turn-6-deg-per-s": partial(turn, turn_rate_deg_s=6),
    "turn-12-deg-per-s": partial(turn, turn_rate_deg_s=12),
    "turn-24-deg-per-s": partial(turn, turn_rate_deg_s=24),
}
