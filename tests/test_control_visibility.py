"""Exercise actual MAVLink encoding, outbound capture, and link-expiry behavior."""
import io
import json
import os
from pathlib import Path
import tempfile
import time
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from mcap.reader import make_reader
from pymavlink import mavutil
from backend.controller.vehicle_control import VehicleController
from backend.controller.runtime import ControllerRuntime
from backend.rerun.schema import field_catalog
from backend.bridge.runtime import MissionBackend
from backend.mapping.tiles import RasterTiles


class ControlVisibilityTests(unittest.TestCase):
    def test_idle_sdk_tick_preserves_joystick_output_and_neutral_ticks(self):
        with patch("backend.mavlink.transport.SessionRecorder"):
            controller = VehicleController()
        runtime = ControllerRuntime(controller, Mock(), inputs=False)
        sender = mavutil.mavlink.MAVLink(io.BytesIO())
        controller.master = SimpleNamespace(mav=sender, target_system=1, target_component=1)
        controller.connected = True
        controller.mode_str = "MANUAL"
        runtime.command("manual_active", {"active": True})
        # The input worker ticks the SDK after accepting each joystick sample.
        runtime._accept_gamepad_sample(600, -200, "GAMEPAD")
        for _ in range(3):
            runtime.programmatic.tick()
            runtime.session.manual.send_tick()
        self.assertEqual(sender.total_packets_sent, 6)
        self.assertEqual((controller.last_manual_send["throttle"],
                          controller.last_manual_send["yaw"]), (600, -200))
        runtime._accept_gamepad_sample(0, 0, "GAMEPAD")
        for _ in range(5):
            runtime.programmatic.tick()
            runtime.session.manual.send_tick()
        self.assertEqual(sender.total_packets_sent, 16)
        runtime.programmatic.tick()
        runtime.session.manual.send_tick()
        self.assertEqual(sender.total_packets_sent, 16)

    def test_sdk_ownership_transitions_clear_old_joystick_axes(self):
        with patch("backend.mavlink.transport.SessionRecorder"):
            controller = VehicleController()
        runtime = ControllerRuntime(controller, Mock(), inputs=False)
        manual = runtime.session.manual
        manual.set_axes(600, -200, "GAMEPAD")
        manual.suspend(True)
        self.assertEqual((manual.throttle, manual.yaw, manual.neutral_ticks), (0, 0, 0))
        manual.set_axes(300, 100, "GAMEPAD")
        manual.suspend(False)
        self.assertEqual((manual.throttle, manual.yaw, manual.neutral_ticks), (0, 0, 0))

    def test_control_pair_is_recorded_as_sent_with_expected_wire_fields(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"MISSION_CONTROL_DATA_DIR": tmp}, clear=True):
            controller = VehicleController()
            encoded = io.BytesIO()
            sender = mavutil.mavlink.MAVLink(encoded, srcSystem=255, srcComponent=190)
            sender.set_send_callback(controller.transport.capture.sent)
            controller.master = SimpleNamespace(mav=sender, target_system=3, target_component=1)
            controller.connected = True
            controller.recorder.open()
            self.assertTrue(controller.send_manual_control(1000, -500))
            controller.recorder.close()
            with controller.recorder.output_path.open("rb") as handle:
                records = list(make_reader(handle, validate_crcs=True).iter_messages())
            self.assertEqual({channel.topic for _, channel, _ in records},
                             {"/commands/MANUAL_CONTROL", "/commands/RC_CHANNELS_OVERRIDE"})
            payloads = {json.loads(message.data)["msg_type"]: json.loads(message.data) for _, _, message in records}
            self.assertTrue(all(payload["direction"] == "tx" for payload in payloads.values()))
            self.assertTrue(all(payload["sys_id"] == 255 for payload in payloads.values()))
            manual = payloads["MANUAL_CONTROL"]["data"]
            rc = payloads["RC_CHANNELS_OVERRIDE"]["data"]
            self.assertEqual((manual["x"], manual["r"], manual["y"], manual["z"]), (1000, -500, 0, 0))
            self.assertEqual((rc["chan1_raw"], rc["chan3_raw"]), (1250, 2000))
            self.assertTrue(controller.last_manual_send["sent"])
            schema = field_catalog({"/commands/MANUAL_CONTROL": {"x", "r"}})
            observed = [field for field in schema if field["kind"] == "raw" and field["available"]]
            self.assertEqual(len(observed), 2)
            self.assertTrue(all(field["direction"] == "sent" for field in observed))

    def test_sent_fields_reach_live_rerun_catalog_while_observer_runs(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"MISSION_CONTROL_DATA_DIR": tmp}, clear=True):
            controller = VehicleController()
            root = Path(__file__).resolve().parents[1]
            backend = MissionBackend(controller, RasterTiles(root/"assets/fiu-basemap", tmp), None, demo=True)
            sender = mavutil.mavlink.MAVLink(io.BytesIO())
            delivered = threading.Event()
            def capture(message):
                controller.transport.capture.sent(message)
                if message.get_type() == "RC_CHANNELS_OVERRIDE":
                    delivered.set()
            sender.set_send_callback(capture)
            controller.master = SimpleNamespace(mav=sender, target_system=3, target_component=1, close=lambda: None)
            backend.start()
            try:
                controller.mode_str = "MANUAL"
                controller.connected = True
                backend.command("manual_active", {"active": True})
                backend._input_queue.put((600, 200, "GAMEPAD"))
                self.assertTrue(delivered.wait(timeout=2), "Control streamer did not deliver a pair")
                controller.recorder.flush()
                state = backend.state(refresh_ui=False)
                self.assertIn("/commands/MANUAL_CONTROL", state["sensor_topics"])
                self.assertIn("chan3_raw", state["sensor_topics"]["/commands/RC_CHANNELS_OVERRIDE"])
            finally:
                backend.close()
            self.assertTrue(backend.live_recording.is_file())

    def test_stale_heartbeat_expires_manual_input_despite_fresh_ui(self):
        with patch("backend.mavlink.transport.SessionRecorder"):
            controller = VehicleController()
        service = ControllerRuntime(controller, Mock(), inputs=False)
        now = time.monotonic()
        controller.connected = True
        controller.mode_str = "MANUAL"
        controller._last_vehicle_heartbeat = now - 4
        service._last_ui = service._last_gamepad = now
        service._manual_active = True
        service.session.manual.set_axes(1000, 500, "GAMEPAD")
        service.expire_inputs(now)
        self.assertFalse(controller.connected)
        self.assertFalse(service._manual_active)
        self.assertEqual((service.session.manual.throttle, service.session.manual.yaw), (0, 0))
        controller.master = Mock()
        self.assertFalse(controller.send_manual_control(1000, 500))
        controller.master.mav.manual_control_send.assert_not_called()

    def test_recording_failure_does_not_interrupt_control_pair(self):
        with patch("backend.mavlink.transport.SessionRecorder"):
            controller = VehicleController()
        sender = mavutil.mavlink.MAVLink(io.BytesIO())
        sender.set_send_callback(controller.transport.capture.sent)
        controller.master = SimpleNamespace(mav=sender, target_system=3, target_component=1)
        controller.connected = True
        controller.recorder.log_message.side_effect = OSError("disk full")
        self.assertTrue(controller.send_manual_control(2000, -2000))
        self.assertEqual(sender.total_packets_sent, 2)
        self.assertEqual(controller.recording_error, "disk full")
        self.assertEqual((controller.last_manual_send["throttle"], controller.last_manual_send["yaw"]), (1000, -1000))
