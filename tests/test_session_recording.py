"""Verify session bundles, independent RRD persistence, and graceful process termination."""
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from mcap.reader import make_reader
from pymavlink import mavutil
import rerun as rr
from backend.bridge.runtime import MissionBackend
from backend.controller.vehicle_control import VehicleController
from backend.mapping.tiles import RasterTiles
from backend.rerun.viewer_stream import ViewerStream

ROOT = Path(__file__).resolve().parents[1]


def verify_rrd(path):
    result = subprocess.run([sys.executable, "-m", "rerun", "rrd", "verify", str(path)],
                            capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise AssertionError(result.stderr)


class SessionRecordingTests(unittest.TestCase):
    def test_viewer_disconnect_does_not_prevent_valid_rrd_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"session.rrd"
            stream = ViewerStream(path)
            stream.viewer = Mock()
            stream.viewer.flush.side_effect = RuntimeError("gRPC connection severed: viewer closed")
            stream.set_time("mission", duration=0)
            stream.log("sensor/value", rr.Scalars(12.5))
            self.assertFalse(path.exists())
            self.assertTrue(stream.active_path.exists())
            stream.flush()
            stream.disconnect()
            stream.disconnect()
            self.assertTrue(path.exists())
            self.assertFalse(stream.active_path.exists())
            stream.viewer.disconnect.assert_called_once()
            verify_rrd(path)

    def test_formats_share_one_session_folder_and_payloads_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"MISSION_CONTROL_DATA_DIR": tmp}, clear=True):
            controller = VehicleController()
            backend = MissionBackend(controller, RasterTiles(ROOT/"assets/fiu-basemap", tmp), None, demo=True)
            backend.start()
            logger = controller.recorder
            try:
                encoder = mavutil.mavlink.MAVLink(None, srcSystem=3, srcComponent=1)
                message = encoder.global_position_int_encode(1000, 257591000, -803716000, 2000, 0, 300, 400, 0, 9000)
                message.pack(encoder)
                logger.log_message(message)
                self.assertEqual(backend.live_recording.parent, logger.output_path.parent)
                self.assertEqual(backend.live_recording.stem, logger.output_path.stem)
                self.assertEqual(logger.json_path.stem, logger.output_path.stem)
                self.assertFalse(logger.output_path.exists())
                self.assertFalse(backend.live_recording.exists())
                self.assertTrue(backend.telemetry.stream.active_path.exists())
            finally:
                backend.close()
            directory = logger.output_path.parent
            self.assertEqual(directory.parent, Path(tmp)/"logs")
            self.assertEqual({path.suffix for path in directory.iterdir()}, {".mcap", ".rrd", ".jsonl"})
            self.assertFalse((Path(tmp)/"recordings").exists())
            self.assertFalse((Path(tmp)/"blueprints").exists())
            with logger.output_path.open("rb") as handle:
                messages = list(make_reader(handle, validate_crcs=True).iter_messages())
            self.assertEqual(len(messages), 1)
            self.assertEqual(json.loads(logger.json_path.read_text()), json.loads(messages[0][2].data))
            verify_rrd(backend.live_recording)

    def test_sigterm_finalizes_all_session_files(self):
        import socket
        with tempfile.TemporaryDirectory() as tmp:
            sink = rr.RecordingStream("signal-test-receiver")
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            url = sink.serve_grpc(grpc_port=port)
            diagnostics = Path(tmp)/"stderr"
            with diagnostics.open("w") as stderr:
                env = dict(os.environ, MISSION_CONTROL_DATA_DIR=tmp, MISSION_CONTROL_LOG_DIR=str(Path(tmp)/"logs"), MISSION_CONTROL_CACHE_DIR=tmp)
                process = subprocess.Popen([sys.executable, "-u", "-m", "backend.bridge", "--demo", "--rerun-url", url],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, text=True, env=env)
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        self.assertTrue(selector.select(timeout=15), diagnostics.read_text())
                    ready = json.loads(process.stdout.readline())
                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=10)
                    self.assertEqual(process.returncode, 0, diagnostics.read_text())
                    sessions = list((Path(tmp)/"logs").iterdir())
                    self.assertEqual(len(sessions), 1)
                    paths = list(sessions[0].iterdir())
                    diagnostic_log = Path(ready["diagnostic_log"])
                    self.assertEqual(diagnostic_log.parent, sessions[0])
                    self.assertTrue(diagnostic_log.name.endswith(".backend.log"))
                    self.assertIn(diagnostic_log, paths)
                    self.assertEqual({path.suffix for path in paths if path != diagnostic_log}, {".mcap", ".rrd", ".jsonl"})
                    with next(path for path in paths if path.suffix == ".mcap").open("rb") as handle:
                        messages = list(make_reader(handle, validate_crcs=True).iter_messages())
                        self.assertTrue(all(channel.topic == "/input/JOYSTICK" for _, channel, _ in messages))
                    verify_rrd(next(path for path in paths if path.suffix == ".rrd"))
                    self.assertNotIn("Traceback", diagnostics.read_text())
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    process.stdin.close()
                    process.stdout.close()
                    sink.disconnect()
