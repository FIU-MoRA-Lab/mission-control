"""Mission Control: a native Rerun application with a Python vehicle backend."""
from backend.paths import ROOT, FRONTEND
import argparse
import os
import subprocess
import sys


def launch_viewer(args=(), *, attach=None):
    env = os.environ.copy()
    env['MISSION_CONTROL_PYTHON'] = sys.executable
    env['MISSION_CONTROL_ROOT'] = str(ROOT)
    if attach is not None:
        env['MISSION_CONTROL_ATTACH'] = str(attach)
    else:
        env.pop('MISSION_CONTROL_ATTACH', None)
    binary = FRONTEND / 'target/release/mission-control-viewer'
    sources = [*FRONTEND.rglob('*.rs'), *(ROOT / 'boats').rglob('*.rs'), FRONTEND / 'Cargo.toml']
    fresh = binary.exists() and all(path.stat().st_mtime <= binary.stat().st_mtime for path in sources if 'target' not in path.parts)
    command = [str(binary)] if fresh and env.get("MISSION_CONTROL_CUSTOM_MAP") != "1" else [
        'cargo', 'run', '--release', '--manifest-path', str(FRONTEND / 'Cargo.toml'), '--']
    return subprocess.Popen([*command, *args], env=env, cwd=ROOT, start_new_session=os.name != "nt")


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--boat', choices=('heron', 'surveyor'), default='heron')
    selection, args = parser.parse_known_args()
    if selection.boat == 'surveyor':
        parser.error('SeaRobotics Surveyor integration pending; use --boat heron.')
    if args[:1] == ['run']:
        from boats.heron.backend.batch import main as run_batch
        raise SystemExit(run_batch(args[1:]))
    process = launch_viewer(args)
    try:
        raise SystemExit(process.wait())
    except KeyboardInterrupt:
        # Keep the application alive to handle its native quit confirmation.
        print('Use the Mission Control window to quit.', file=sys.stderr)
        raise SystemExit(process.wait())
