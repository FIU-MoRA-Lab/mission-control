"""Fixed mean demand of 30%, with increasing left/right differences."""
from functools import partial
from experiments._common import thrust, steady

VISIT_END = False  # Circle maneuvers return directly to the start/home point.


def turn(mc, difference):
    with thrust(mc) as control:
        steady(control, 5, .3 + difference / 2, .3 - difference / 2)


EXPERIMENTS = {
    "turn-difference-10": partial(turn, difference=.1),
    "turn-difference-20": partial(turn, difference=.2),
    "turn-difference-40": partial(turn, difference=.4),
}
