"""Shared test corridor; generic motion primitives remain in the SDK."""
import json
from pathlib import Path
from backend.sdk import Waypoint

CONFIG = json.loads(Path(__file__).with_name("batch.json").read_text())
HOME = START = Waypoint(**CONFIG["home"])
END = Waypoint(**CONFIG["end"])


def thrust(mc):
    return mc.control(kind="thrusters", start=HOME, end=END, heading_deg=90)


def inputs(left, right):
    return {"left": left, "right": right}


def steady(control, seconds, left, right):
    control.play([(0,inputs(0,0)),(.5,inputs(left,right)),
                  (seconds-.5,inputs(left,right)),(seconds,inputs(0,0))])
