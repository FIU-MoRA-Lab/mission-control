"""Mission Control: a native Rerun application with a Python vehicle backend."""
from backend.paths import ROOT, FRONTEND
import os
import subprocess
import sys


def main():
    root = ROOT
    env = os.environ.copy()
    env['MISSION_CONTROL_PYTHON'] = sys.executable
    env['MISSION_CONTROL_ROOT'] = str(root)
    binary = FRONTEND / 'target/release/mission-control-viewer'
    command = [str(binary)] if binary.exists() and env.get("MISSION_CONTROL_CUSTOM_MAP") != "1" else [
        'cargo', 'run', '--manifest-path', str(FRONTEND / 'Cargo.toml'), '--']
    # The native app owns backend shutdown and armed-vehicle quit confirmation.
    process = subprocess.Popen([*command, *sys.argv[1:]], env=env, start_new_session=os.name != "nt")
    try:
        raise SystemExit(process.wait())
    except KeyboardInterrupt:
        # Keep the application alive to handle its native quit confirmation.
        print('Use the Mission Control window to quit.', file=sys.stderr)
        raise SystemExit(process.wait())
