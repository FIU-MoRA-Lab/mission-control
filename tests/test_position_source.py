"""Verify live position selection without a vehicle connection."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from boats.heron.backend.controller.vehicle_control import VehicleController
from boats.heron.backend.mavlink.telemetry import VehicleTelemetry


class PositionSourceTests(unittest.TestCase):
    def test_only_global_position_updates_coordinates_and_all_messages_are_logged(self):
        with patch("boats.heron.backend.mavlink.transport.SessionRecorder"):
            controller = VehicleController()
        controller.connected = True
        messages = []
        positions = []

        def receive(**kwargs):
            if not messages:
                controller.running = False
                return None
            message = messages.pop(0)
            positions.append((controller.lat, controller.lon))
            return message

        controller.master = SimpleNamespace(recv_match=receive)
        types = ["GPS_RAW_INT", "HOME_POSITION", "AHRS2", "GLOBAL_POSITION_INT",
                 "GPS_RAW_INT", "GPS2_RAW", "AHRS3", "HOME_POSITION",
                 "GLOBAL_POSITION_INT", "GPS_RAW_INT"]
        for index, kind in enumerate(types):
            message = Mock()
            message.get_type.return_value = kind
            message.lat = 0 if index == 8 else 257500000 + index
            message.lon = -803000000 - index
            message.lng = message.lon
            message.latitude = message.lat
            message.longitude = message.lon
            message.alt = 1000
            message.hdg = 9000
            message.vx, message.vy = 30, 40
            messages.append(message)
        original = list(messages)
        controller.transport.master = controller.master
        VehicleTelemetry(controller).run()

        self.assertEqual(positions[:4], [(0.0, 0.0)] * 4)
        estimated = (original[3].lat / 1e7, original[3].lon / 1e7)
        self.assertEqual(positions[4:9], [estimated] * 5)
        self.assertEqual(positions[9], (0.0, original[8].lon / 1e7))
        self.assertEqual((controller.lat, controller.lon), positions[9])
        self.assertEqual(controller.groundspeed, .5)
        self.assertGreater(controller._last_speed_at, 0)
        self.assertEqual([call.args[0] for call in controller.recorder.log_message.call_args_list], original)


if __name__ == "__main__":
    unittest.main()
