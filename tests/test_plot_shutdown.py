"""Regression coverage for shutdown disconnects and focused recording plots."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from mcap.writer import Writer
from boats.heron.backend.bridge.runtime import MissionBackend
from boats.heron.backend.controller.runtime import ControllerRuntime
from boats.heron.backend.mavlink.telemetry_values import telemetry_fields
from boats.heron.visualization.replay import export_mcap
from boats.heron.visualization.schema import field_catalog, DEFAULT_FIELDS


def recording_fixture(path):
    with path.open("wb") as handle:
        writer = Writer(handle)
        writer.start()
        for topic, message_type, data in [
            ("/vehicle/GLOBAL_POSITION_INT", "GLOBAL_POSITION_INT",
             {"lat": 257591000, "lon": -803716000, "alt": 2000, "hdg": 9000, "vx": 300, "vy": 400}),
            ("/sensors/water", "WATER", {"temperature": 24.5, "conductivity": 410}),
            ("/vehicle/SYS_STATUS", "SYS_STATUS", {"voltage_battery": 12500, "battery_remaining": 85}),
            ("/mavlink/RC_CHANNELS", "RC_CHANNELS", {"chan1_raw": 1450, "chan3_raw": 1650}),
            ("/mavlink/SERVO_OUTPUT_RAW", "SERVO_OUTPUT_RAW", {"servo1_raw": 1600, "servo3_raw": 1700}),
        ]:
            channel = writer.register_channel(topic=topic, message_encoding="json", schema_id=0)
            for i in range(3):
                timestamp = 1_000_000_000 + i * 100_000_000
                writer.add_message(channel_id=channel, log_time=timestamp, publish_time=timestamp,
                                   data=json.dumps({"msg_type": message_type, "data": data}).encode())
        writer.finish()


class PlotShutdownTests(unittest.TestCase):
    def backend_stub(self):
        backend = MissionBackend.__new__(MissionBackend)
        backend._telemetry_lock = threading.RLock()
        backend._replay_lock = threading.Lock()
        backend._sensor_fields = {"WATER": {"temperature", "conductivity"}}
        backend._field_types = {}
        backend._replay_plots = None
        backend._viewer_catalogs = {}
        backend._telemetry_closed = False
        backend._stop = threading.Event()
        backend._telemetry_worker = Mock()
        backend._telemetry_worker.is_alive.return_value = False
        backend.telemetry = Mock()
        backend.camera = Mock()
        return backend

    def test_disconnect_during_close_releases_stream_and_close_is_idempotent(self):
        backend = self.backend_stub()
        backend.telemetry.stream.flush.side_effect = RuntimeError("gRPC connection severed: viewer closed")
        with patch.object(ControllerRuntime, "close"):
            backend.close()
            backend.close()
        backend.telemetry.stream.disconnect.assert_called_once()
        backend.telemetry.stream.flush.assert_called_once()

    def test_other_flush_errors_are_reported_but_disconnect_still_runs(self):
        backend = self.backend_stub()
        backend.telemetry.stream.flush.side_effect = RuntimeError("file write failed")
        with patch.object(ControllerRuntime, "close"), self.assertRaisesRegex(RuntimeError, "file write"):
            backend.close()
        backend.telemetry.stream.disconnect.assert_called_once()

    def test_layer_selection_rejects_unknown_fields_and_allows_independent_visibility(self):
        backend = self.backend_stub()
        for data in [{"source": "unknown"}, {"source": "replay", "visible_fields": []},
                     {"visible_fields": "all"}, {"visible_fields": ["unknown"]}, {"visible_fields": [42]}]:
            with self.assertRaises(ValueError):
                backend.command("configure_plots", data)
        for fields in [["/sensors/WATER/temperature", "/telemetry/battery_v"], []]:
            backend.command("configure_plots", {"visible_fields": fields})
        self.assertEqual(backend.telemetry.stream.send_blueprint.call_count, 2)

    def test_presets_target_the_selected_viewer_recording_instead_of_latest_replay(self):
        backend = self.backend_stub()
        backend.rerun_url = "rerun+http://localhost:1234/proxy"
        # The startup replay and a file opened directly in Rerun have distinct identities.
        first = backend.command("recording_catalog", {
            "application_id": "startup", "recording_id": "first", "entities": [
                {"path": "/mavlink/RC_CHANNELS/chan3_raw", "data_type": "number"},
            ],
        })
        backend._replay_plots = first
        second = backend.command("recording_catalog", {
            "application_id": "opened-in-viewer", "recording_id": "second", "entities": [
                {"path": "/mavlink/SERVO_OUTPUT_RAW/servo1_raw", "data_type": "number"},
                {"path": "/vehicle/mode", "data_type": "state"},
                {"path": "/input/JOYSTICK/active", "data_type": "bool"},
            ],
        })
        self.assertIn("/vehicle/mode", next(item for item in second["chart_presets"] if item["id"] == "state_changes")["visible_fields"])
        with patch("boats.heron.backend.bridge.runtime.rr.RecordingStream") as stream:
            for info, preset in [(second, "throttle"), (first, "throttle"), (second, "state_changes")]:
                fields = next(item for item in info["chart_presets"] if item["id"] == preset)["visible_fields"]
                backend.command("configure_plots", dict(source="replay", application_id=info["application_id"],
                                                        recording_id=info["recording_id"], preset=preset, visible_fields=fields))
                stream.assert_called_with(info["application_id"], recording_id=info["recording_id"])
                stream.return_value.send_blueprint.assert_called()
                stream.return_value.disconnect.assert_called()
            stream.reset_mock()
            # A stale or unavailable identity must never fall back to the startup recording.
            with self.assertRaisesRegex(ValueError, "catalog is not ready"):
                backend.command("configure_plots", dict(source="replay", application_id="unknown", recording_id="gone", visible_fields=[]))
            stream.assert_not_called()

    def test_schema_uses_protocol_units_and_defaults_to_control_outputs(self):
        schema = field_catalog({"GPS_RAW_INT": {"fix_type", "lat"}, "SYS_STATUS": {"voltage_battery"},
                                "/sensors/water": {"temperature"}}, units={("/sensors/water", "temperature"): "°C"})
        lookup = {field["source"]: field for field in schema}
        self.assertEqual(lookup["GPS_RAW_INT.lat"]["unit"], "degE7")
        self.assertEqual(lookup["GPS_RAW_INT.fix_type"]["enum"], "GPS_FIX_TYPE")
        self.assertEqual(lookup["SYS_STATUS.voltage_battery"]["data_type"], "uint16_t")
        self.assertEqual(lookup["SYS_STATUS.voltage_battery"]["unit"], "mV")
        self.assertEqual(lookup["/sensors/water.temperature"]["unit"], "°C")
        self.assertEqual([field["id"] for field in schema if field["default"]], DEFAULT_FIELDS)
        self.assertFalse(lookup["SYS_STATUS.voltage_battery"]["default"])
        self.assertFalse(lookup["/sensors/water.temperature"]["default"])

    def test_telemetry_units_and_unknown_values(self):
        self.assertEqual(telemetry_fields("GLOBAL_POSITION_INT", {"alt": 2000, "hdg": 9000, "vx": 300, "vy": 400}),
                         {"alt": 2, "heading": 90, "speed": 5})
        self.assertEqual(telemetry_fields("SYS_STATUS", {"voltage_battery": 65535, "battery_remaining": -1}), {})
        self.assertEqual(telemetry_fields("GPS_RAW_INT", {"satellites_visible": 255, "fix_type": 3}), {"fix": 3})

    def test_export_retains_raw_sensor_catalog_and_valid_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp)/"input.mcap", Path(tmp)/"output.rrd"
            recording_fixture(source)
            basemap = {"crs": "EPSG:4326", "bounds": [-81, 25, -80, 26], "center": [-80.37, 25.76]}
            result = export_mcap(source, output, basemap, {"features": []})
            self.assertEqual(result["sensor_topics"]["/sensors/water"], ["conductivity", "temperature"])
            self.assertIn("voltage_battery", result["sensor_topics"]["/vehicle/SYS_STATUS"])
            self.assertTrue(result["recording_id"])
            self.assertEqual(result["visible_fields"], DEFAULT_FIELDS)
            check = subprocess.run([sys.executable, "-m", "rerun", "rrd", "verify", str(output)], capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)

    def test_managed_bridge_filters_replay_and_exits_cleanly(self):
        import rerun as rr
        import socket
        import selectors
        from urllib.request import Request, urlopen
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/"input.mcap"
            recording_fixture(source)
            sink = rr.RecordingStream("regression-receiver")
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            url = sink.serve_grpc(grpc_port=port)
            diagnostics = Path(tmp)/"stderr"
            with diagnostics.open("w") as stderr:
                env = dict(os.environ, MISSION_CONTROL_DATA_DIR=tmp, MISSION_CONTROL_CACHE_DIR=tmp, HERON_LOG_DIR=tmp)
                process = subprocess.Popen([sys.executable, "-u", "-m", "boats.heron.backend.bridge", "--demo", "--rerun-url", url],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, text=True, env=env)
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        self.assertTrue(selector.select(timeout=15), diagnostics.read_text())
                    ready = json.loads(process.stdout.readline())
                    def request(path, data):
                        req = Request(f"http://127.0.0.1:{ready['port']}{path}", data=json.dumps(data).encode(),
                                      headers={"Authorization": f"Bearer {ready['token']}", "Content-Type": "application/json"})
                        with urlopen(req, timeout=15) as response:
                            return json.load(response)
                    replay = request("/command", {"action": "load_mcap", "path": str(source)})
                    temperature = next(field["id"] for field in replay["field_catalog"] if field["source"] == "/sensors/water.temperature")
                    request("/command", {"action": "configure_plots", "source": "replay", "visible_fields": [temperature, "/telemetry/battery_v"]})
                    request("/command", {"action": "filter_readings", "topic": "/sensors/water", "field": "temperature",
                                         "minimum": 24, "maximum": 25})
                    request("/command", {"action": "configure_plots", "source": "replay", "visible_fields": []})
                    reopened = request("/command", {"action": "load_recording", "path": replay["rerun_path"]})
                    self.assertEqual(reopened["format"], "rrd")
                    self.assertNotEqual(reopened["recording_id"], replay["recording_id"])
                    self.assertIn(temperature, {field["id"] for field in reopened["field_catalog"]})
                    request("/command", {"action": "configure_plots", "source": "replay", "visible_fields": [temperature]})
                    request("/shutdown", {"confirmed_armed": False})
                    process.wait(timeout=8)
                    self.assertEqual(process.returncode, 0, diagnostics.read_text())
                    self.assertNotIn("Traceback", diagnostics.read_text())
                    self.assertNotIn("gRPC connection severed", diagnostics.read_text())
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    process.stdin.close()
                    process.stdout.close()
                    sink.disconnect()
