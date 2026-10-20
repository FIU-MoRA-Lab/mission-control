"""Cover idle GCS presence and mission start confirmation using encoded MAVLink."""
import io
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pymavlink import mavutil
from backend.controller.vehicle_control import VehicleController
from backend.mavlink.transport import MavlinkTransport
from backend.mavlink.telemetry import VehicleTelemetry


class LiveMissionControlTests(unittest.TestCase):
    def controller(self):
        with patch("backend.mavlink.transport.SessionRecorder"):
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
