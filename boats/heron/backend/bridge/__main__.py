"""Managed Python process started by boats/heron/frontend/bridge/worker.rs."""
import argparse
from contextlib import ExitStack
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import sys
import threading
from platformdirs import user_cache_path
from boats.heron.paths import ROOT, ASSETS, data_directory, log_directory
from boats.heron.backend.controller.vehicle_control import VehicleController
from backend.mapping.tiles import RasterTiles
from backend.bridge.http_server import make_server
from boats.heron.backend.bridge.runtime import MissionBackend
from boats.heron.backend.sdk.discovery import publish_endpoint

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rerun-url")
    parser.add_argument("--no-gps-test",action="store_true",help="Motor testing only: no geographic checks or navigation")
    parser.add_argument("--no-inputs", action="store_true")
    parser.add_argument("--execution-mode", choices=("simulation", "live"))
    parser.add_argument("--connection", default="udpin:0.0.0.0:14550")
    parser.add_argument("--mission-file")
    parser.add_argument("--fiu-root", type=Path, default=ASSETS/"fiu-basemap")
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    protocol = sys.stdout
    sys.stdout = sys.stderr
    cache = Path(os.environ.get("MISSION_CONTROL_CACHE_DIR", user_cache_path("mission-control")))
    controller = VehicleController(args.connection)
    # Keep request tracebacks with this session; the native UI only has the
    # HTTP error string and cannot reconstruct why a request failed.
    diagnostic_log = Path(controller.recorder.output_path).with_suffix(".backend.log")
    logging.basicConfig(level=logging.WARNING)
    handler = logging.FileHandler(diagnostic_log)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger("backend").addHandler(handler)
    logging.getLogger("boats.heron").addHandler(handler)
    if args.mission_file and not controller.load_waypoints_file(args.mission_file):
        raise ValueError("Could not load initial mission")
    backend = MissionBackend(controller, RasterTiles(args.fiu_root, cache), args.rerun_url,
                             demo=args.demo, cache=cache, inputs=not args.no_inputs)
    backend.execution_mode = args.execution_mode
    backend.testing_without_gps = args.no_gps_test
    token = secrets.token_urlsafe(32)
    server = make_server(backend, token)
    def parent_watch():
        # A daemon blocked in BufferedReader.read holds a Python buffer lock,
        # which can abort interpreter shutdown after an HTTP quit request.
        while os.read(sys.stdin.fileno(), 1024):
            pass
        threading.Thread(target=server.shutdown, daemon=True).start()
    threading.Thread(target=parent_watch, daemon=True).start()
    def shutdown_signal(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, shutdown_signal)
    discovery = ExitStack()
    try:
        backend.start()
        discovery.enter_context(publish_endpoint(server.server_port, token))
        print(json.dumps({"port": server.server_port, "token": token, "basemap": backend.basemap,
                          "demo": args.demo, "data_directory": str(data_directory()),
                          "log_directory": str(log_directory()),
                          "diagnostic_log": str(diagnostic_log)}), file=protocol, flush=True)
        display_stop = threading.Event()
        os.set_blocking(protocol.fileno(), False)
        def joystick_display():
            # A separate pipe writer can block without blocking HID input or logging.
            while not display_stop.wait(1 / 120):
                try:
                    packet = (json.dumps({"event": "joystick", "data": backend.joystick_state()}) + "\n").encode()
                    os.write(protocol.fileno(), packet)
                except BlockingIOError:
                    continue  # Drop an obsolete display update; next tick sends the latest.
                except (BrokenPipeError, OSError):
                    return
        threading.Thread(target=joystick_display, name="joystick-display", daemon=True).start()
        try:
            server.serve_forever(poll_interval=.1)
        finally:
            display_stop.set()
    finally:
        discovery.close()
        server.server_close()
        backend.close()


if __name__ == "__main__": main()
