"""Heron resources and future output locations; existing data is left in place."""
import os
from datetime import datetime
from pathlib import Path

from backend import paths as shared
from backend.paths import ROOT, ASSETS, FRONTEND, partial_path, publish_file

BOAT_ROOT = ROOT / 'boats' / 'heron'
CONFIG = BOAT_ROOT / 'config'


def data_directory():
    return shared.data_directory() / 'boats' / 'heron'


def output_directory(kind):
    directory = data_directory() / kind
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def log_directory():
    # Explicit overrides are already instance-specific directories.
    override = os.environ.get('MISSION_CONTROL_LOG_DIR') or os.environ.get('HERON_LOG_DIR')
    return Path(override).expanduser() if override else shared.data_directory() / 'logs' / 'heron'


def session_path(label='live'):
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    name = f'{label}_{stamp}'
    directory = log_directory() / name
    directory.mkdir(parents=True, exist_ok=False)
    return directory / name


def recording_path(label='live'):
    return session_path(label).with_suffix('.rrd')
