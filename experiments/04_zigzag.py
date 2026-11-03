"""Continuous differential inputs alternating while retaining mean forward demand."""
from experiments._common import thrust, inputs


def run(mc):
    with thrust(mc) as control:
        control.play([(0,inputs(0,0)),(.5,inputs(.4,.2)),
                      (3,inputs(.2,.4)),(6,inputs(.4,.2)),
                      (9,inputs(.2,.4)),(11.5,inputs(.2,.4)),(12,inputs(0,0))])
