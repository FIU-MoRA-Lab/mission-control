"""Standalone controller bridge without Rerun, retained for socket lifecycle checks."""
import argparse
import json
import os
import secrets
import signal
import sys
import threading
from pathlib import Path
from backend.paths import ROOT, ASSETS
from backend.controller.runtime import ControllerRuntime
from backend.controller.vehicle_control import VehicleController
from backend.mapping.tiles import RasterTiles
from backend.mavlink.network import ensure_gcs_ip, mavlink_connection_string
from backend.bridge.http_server import make_server

def confirm_terminal_quit(armed):
    if not armed:
        return True
    print("Vehicle is armed. Are you sure you want to quit?", file=sys.stderr)
    if not sys.stdin.isatty():
        print("Quit cancelled: an interactive confirmation is required.", file=sys.stderr)
        return False
    try:
        return input("Quit? [y/N] ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False

def main():
    parser = argparse.ArgumentParser(description="Mission Control standalone controller bridge")
    parser.add_argument("--mission-file")
    parser.add_argument("--connection", default=mavlink_connection_string())
    parser.add_argument("--fiu-root", type=Path, default=ASSETS / "fiu-basemap")
    parser.add_argument("--no-ip-setup", action="store_true")
    parser.add_argument("--managed", action="store_true", help="Exit when parent closes stdin")
    args = parser.parse_args()
    protocol_stdout = sys.stdout
    sys.stdout = sys.stderr
    if not args.no_ip_setup:
        ensure_gcs_ip()
    controller = VehicleController(args.connection)
    if args.mission_file:
        if not controller.load_waypoints_file(args.mission_file):
            raise ValueError("Could not load initial mission")
    token = secrets.token_urlsafe(32)
    cache = Path(os.environ.get("HERON_CACHE_DIR", Path.home() / ".cache" / "heron_tiles"))
    backend = ControllerRuntime(controller, RasterTiles(args.fiu_root, cache))
    server = make_server(backend, token)
    def shutdown(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    terminal_quit_pending = False
    def terminal_shutdown(*_):
        nonlocal terminal_quit_pending
        if terminal_quit_pending:
            return
        terminal_quit_pending = True
        try:
            while True:
                status = backend.prepare_quit()
                if not confirm_terminal_quit(status["armed"]):
                    return
                if backend.request_shutdown(confirmed_armed=status["armed"]):
                    shutdown()
                    return
        finally:
            terminal_quit_pending = False
    # The native frontend handles the user's Ctrl+C and keeps the backend alive until the
    # user confirms. Backend stdin EOF remains the unconditional parent-death
    # cleanup path; it cannot depend on a still-running GUI.
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, signal.SIG_IGN if args.managed else terminal_shutdown)
    if args.managed:
        def parent_watch():
            sys.stdin.buffer.read()
            shutdown()
        threading.Thread(target=parent_watch, daemon=True).start()
    backend.start()
    print(json.dumps({"port": server.server_port, "token": token,
                      "python": sys.executable,
                      "source": str(ROOT / "backend")}),
          file=protocol_stdout, flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        backend.close()


if __name__ == "__main__":
    main()
