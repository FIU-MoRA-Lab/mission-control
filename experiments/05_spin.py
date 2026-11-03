"""Opposite 20% demands; requires reverse-capable thrusters for a physical pivot."""
from experiments._common import thrust, steady

VISIT_END = False  # A pivot has no outbound east-waypoint leg.


def run(mc):
    with thrust(mc) as control:
        steady(control, 5, .2, -.2)
