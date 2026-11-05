"""Verify movement feedback and client cancellation without hardware."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from boats.heron.backend.control.motion import Motion
from boats.heron.backend.control.planning import Waypoint
from boats.heron.backend.sdk.client import Control, MissionControl


class MotionTests(unittest.TestCase):
    def test_speed_output_responds_to_measured_speed(self):
        data = {"speed_mps":1, "duration_s":5}
        slow = Motion("move", data, 90)
        fast = Motion("move", data, 90)
        a = slow.step(1, .02, Waypoint(0, 0), 90, .2)
        b = fast.step(1, .02, Waypoint(0, 0), 90, 1.5)
        self.assertGreater(a[0], b[0])
        self.assertEqual(b[0], 0)

    def test_acceleration_and_deceleration_share_the_total_duration(self):
        motion = Motion("move", {"speed_mps":1, "duration_s":10, "acceleration_mps2":.2}, 90)
        for elapsed, expected in ((0, 0), (1, .2), (5, 1), (9, .2)):
            motion.step(elapsed, .02, Waypoint(0, 0), 90, 0)
            self.assertAlmostEqual(motion.target_speed, expected)
        self.assertEqual(motion.step(10, .02, Waypoint(0, 0), 90, .2), (0, 0, True))

    def test_angular_rate_drives_heading_reference_clockwise(self):
        motion = Motion("move", {"speed_mps":.6, "duration_s":5, "turn_rate_deg_s":12}, 350)
        for i in range(20):
            motion.step(i * .1, .1, Waypoint(0, 0), 350, .6)
        self.assertAlmostEqual(motion.heading, 14)

    def test_invalid_units_and_nonfinite_requests_are_rejected(self):
        for changes in ({"speed_mps":True}, {"speed_mps":-1}, {"duration_s":float("inf")},
                        {"heading_deg":float("nan")}, {"turn_rate_deg_s":61},
                        {"acceleration_mps2":0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Motion("move", {"speed_mps":1, "duration_s":5, **changes}, 0)


class MovementClientTests(unittest.TestCase):
    def test_scope_waits_for_neutral_without_requesting_any_movement(self):
        client = MissionControl.__new__(MissionControl)
        client.default_mode = "simulation"
        client._controls = []
        client.state = Mock(side_effect=[{"sdk":{"neutralizing":True}}, {"sdk":{"neutralizing":False}}])
        client._call = Mock(return_value={"id":"c1", "kind":"axes"})
        with patch("boats.heron.backend.sdk.client.time.sleep") as sleep:
            with client.control():
                pass
        sleep.assert_called_once_with(.05)
        self.assertEqual([call.args[0] for call in client._call.call_args_list], ["acquire", "release"])

    def test_interrupted_wait_cancels_only_its_own_movement(self):
        client = SimpleNamespace(_call=Mock(return_value={"motion_id":"m1"}), state=Mock())
        client.state.return_value = {"sdk":{"control":{
            "id":"c1", "status":"active", "motion_id":"m1", "motion_status":"active"}}}
        control = Control(client, {"id":"c1", "kind":"axes"})
        with patch("boats.heron.backend.sdk.client.time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                control.move(speed_mps=1, duration_s=5)
        client._call.assert_called_with("control_stop", id="c1", motion_id="m1")

    def test_replaced_movement_does_not_report_success(self):
        client = SimpleNamespace(_call=Mock(return_value={"motion_id":"m1"}), state=Mock())
        client.state.return_value = {"sdk":{"control":{
            "id":"c1", "status":"active", "motion_id":"m2", "motion_status":"completed"}}}
        control = Control(client, {"id":"c1", "kind":"axes"})
        with self.assertRaisesRegex(RuntimeError, "replaced"):
            control.move(speed_mps=1, duration_s=5)


if __name__ == "__main__":
    unittest.main()
