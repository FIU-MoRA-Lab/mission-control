"""Shared coordinates and settings only; no control commands or movement helpers."""
import json
from pathlib import Path
from backend.sdk import Waypoint

CONFIG = json.loads(Path(__file__).with_name("corridor.json").read_text())
HOME = START = Waypoint(**CONFIG["start"])
END = Waypoint(**CONFIG["end"])
START_HEADING_DEG = CONFIG["heading_deg"]
TRANSIT_SPEED_MPS = CONFIG["speed_mps"]
