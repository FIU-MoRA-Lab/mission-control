"""Shared checkout resource paths; independent of the caller working directory."""
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
FRONTEND = ROOT / "frontend"


def data_directory():
    """Mission Control owns all generated output, independent of working directory."""
    return Path(os.environ.get("MISSION_CONTROL_DATA_DIR", ROOT)).expanduser()

def output_directory(kind):
    directory = data_directory() / kind
    directory.mkdir(parents=True, exist_ok=True)
    return directory

def log_directory():
    """One output root for every session format, including legacy overrides."""
    override = os.environ.get("MISSION_CONTROL_LOG_DIR") or os.environ.get("HERON_LOG_DIR")
    return Path(override).expanduser() if override else data_directory() / "logs"


def session_path(label="live"):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    name = f"{label}_{stamp}"
    directory = log_directory() / name
    directory.mkdir(parents=True, exist_ok=False)
    return directory / name


def recording_path(label="live"):
    return session_path(label).with_suffix(".rrd")


def partial_path(path):
    path = Path(path)
    return path.with_name(path.name + ".part")


def publish_file(active, destination):
    """Publish a closed, flushed file without exposing an unfinished recording."""
    with Path(active).open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(active, destination)
