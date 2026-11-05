"""Cover idle GCS presence and mission start confirmation using encoded MAVLink."""
import io
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pymavlink import mavutil
from boats.heron.backend.controller.vehicle_control import VehicleController
from boats.heron.backend.mavlink.transport import MavlinkTransport
from boats.heron.backend.mavlink.telemetry import VehicleTelemetry


class LiveMissionControlTests(unittest.TestCase):
    def controller(self):
        with patch("boats.heron.backend.mavlink.transport.SessionRecorder"):
            controller = VehicleController()
        controller.connected = True
        controller.master = SimpleNamespace(target_system=1, target_component=0)
        controller.autopilot_component = 1
        controller.waypoints = [{"lat": 25.75, "lon": -80.37}]
        controller.mission_uploaded = True
        return controller

    def test_heartbeat_continues_without_manual_packets_and_stops_on_close(self):
        wire = io.BytesIO()
        sender = mavutil.mavlink.MAVLink(wire, srcSystem=255, srcComponent=190)
        received = threading.Event()
        heartbeats = []
        def capture(message):
            heartbeats.append(message)
            if len(heartbeats) == 2:
                received.set()
        sender.set_send_callback(capture)
        transport = MavlinkTransport("unused", recorder=Mock())
        transport.master = SimpleNamespace(mav=sender, close=Mock())
        transport.thread = threading.Thread(target=lambda: None)
        transport.start()
        try:
            self.assertTrue(received.wait(2.5), "Idle/AUTO needs repeated GCS heartbeats")
        finally:
            transport.close()
        self.assertFalse(transport._heartbeat_thread.is_alive())
        parser = mavutil.mavlink.MAVLink(None)
        decoded = parser.parse_buffer(wire.getvalue())
        self.assertGreaterEqual(len(decoded), 2)
        for message in decoded:
            self.assertEqual(message.get_type(), "HEARTBEAT")
            self.assertEqual(message.type, mavutil.mavlink.MAV_TYPE_GCS)
            self.assertEqual(message.autopilot, mavutil.mavlink.MAV_AUTOPILOT_INVALID)

    def test_start_selects_plan_and_waits_for_real_vehicle_state(self):
        controller = self.controller()
        def respond(kind, *args):
            if kind == "set_mode":
                controller._confirmed_mode = "AUTO"
            elif kind == "command_long":
                controller._confirmed_armed = True
                controller._mission_current_seq = 2
                controller._mission_current_at = time.monotonic()
        with patch.object(controller.transport, "send", side_effect=respond) as send:
            controller.master.mode_mapping = lambda: {"AUTO": 10}
            controller._start_mission_worker()
        self.assertEqual(send.call_args_list[0].args, ("mission_set_current", 1, 1, 1))
        self.assertTrue(controller.mission_started)

    def test_sent_commands_alone_cannot_confirm_mission_start(self):
        controller = self.controller()
        controller.mode_str = "AUTO"
        controller.is_armed = True
        self.assertFalse(controller._wait_for_mission_start(time.monotonic(), timeout=0))
        controller._confirmed_mode = "AUTO"
        controller._confirmed_armed = True
        controller._mission_current_at = time.monotonic()
        controller._mission_current_seq = 0
        self.assertFalse(controller._wait_for_mission_start(0, timeout=0))

    def test_unconfirmed_start_is_reported_as_error(self):
        controller = self.controller()
        with patch.object(controller.transport, "send"), \
             patch.object(controller, "set_mode", return_value=True), \
             patch.object(controller, "set_arm", return_value=True), \
             patch.object(controller, "_wait_for_mission_start", return_value=False):
            controller._start_mission_worker()
        self.assertFalse(controller.mission_started)
        self.assertTrue(controller.status_is_error)
        self.assertIn("unconfirmed", controller.last_status_msg)

    def test_kill_switch_sends_repeated_neutral_in_manual_and_auto(self):
        for mode in ("MANUAL", "AUTO"):
            with self.subTest(mode=mode):
                controller = self.controller()
                wire = io.BytesIO()
                controller.master.mav = mavutil.mavlink.MAVLink(wire)
                controller.master.mode_mapping = lambda: {"MANUAL": 0, "AUTO": 10}
                controller.mode_str = mode
                controller.mission_started = mode == "AUTO"
                controller._control_parameters = {"RC1_TRIM": (1490, 0), "RC3_TRIM": (1510, 0)}
                controller.emergency_stop()
                deadline = time.monotonic() + 2
                while controller._stop_in_progress and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertFalse(controller._stop_in_progress)
                self.assertTrue(controller.emergency_stopped)
                self.assertFalse(controller.mission_started)
                self.assertFalse(controller.send_manual_control(600, 200))
                self.assertFalse(controller.set_mode("AUTO"))
                self.assertFalse(controller.set_arm(True))
                decoded = mavutil.mavlink.MAVLink(None).parse_buffer(wire.getvalue())
                modes = [m for m in decoded if m.get_type() == "SET_MODE"]
                manual = [m for m in decoded if m.get_type() == "MANUAL_CONTROL"]
                rc = [m for m in decoded if m.get_type() == "RC_CHANNELS_OVERRIDE"]
                self.assertEqual(len(modes), 5)
                self.assertTrue(all(m.custom_mode == 0 for m in modes))
                self.assertEqual(len(manual), 5)
                self.assertTrue(all((m.x, m.r) == (0, 0) for m in manual))
                self.assertEqual(len(rc), 5)
                self.assertTrue(all((m.chan1_raw, m.chan3_raw) == (1490, 1510) for m in rc))

    def test_kill_switch_prevents_pending_mission_start(self):
        controller = self.controller()
        controller.master.mode_mapping = lambda: {"MANUAL": 0, "AUTO": 10}
        def stop_during_upload():
            controller.emergency_stop()
            return True
        controller.mission_uploaded = False
        with patch.object(controller, "upload_mission", side_effect=stop_during_upload), \
             patch.object(controller.transport, "send") as send:
            controller._start_mission_worker()
            deadline = time.monotonic() + 2
            while controller._stop_in_progress and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertFalse(controller.mission_started)
        self.assertFalse(any(call.args[0] == "command_long" for call in send.call_args_list))
        self.assertFalse(any(call.args[0] == "set_mode" and call.args[-1] == 10 for call in send.call_args_list))

    def test_vehicle_warning_is_exposed_to_operator(self):
        controller = self.controller()
        message = SimpleNamespace(get_type=lambda: "STATUSTEXT", text=b"GCS Failsafe\x00", severity=4)
        def receive(**kwargs):
            controller.running = False
            return message
        controller.master.recv_match = receive
        VehicleTelemetry(controller).run()
        self.assertEqual(controller.last_status_msg, "[Vehicle] GCS Failsafe")
        self.assertTrue(controller.status_is_error)
