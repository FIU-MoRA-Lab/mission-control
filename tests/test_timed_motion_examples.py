"""Run timed examples through the no-GPS backend with mocked motor transport."""
from contextlib import contextmanager
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from boats.heron.backend.batch import discover
from boats.heron.backend.controller.runtime import ControllerRuntime
from boats.heron.backend.controller.vehicle_control import VehicleController
from boats.heron.paths import ROOT


class TimedMotionTests(unittest.TestCase):
    def setUp(self):
        self.now = time.monotonic()
        clock = patch("boats.heron.backend.control.service.time.monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        with patch("boats.heron.backend.mavlink.transport.SessionRecorder"):
            self.vehicle = VehicleController()
        self.vehicle.connected = True
        self.vehicle.mode_str = self.vehicle._confirmed_mode = "MANUAL"
        self.vehicle._last_vehicle_heartbeat = time.monotonic()
        self.vehicle.master = SimpleNamespace(target_system=1, target_component=1)
        self.runtime = ControllerRuntime(self.vehicle, SimpleNamespace(last_error=None), inputs=False)
        self.runtime.testing_without_gps = True
        self.service = self.runtime.programmatic
        self.transport = patch.object(self.vehicle.transport, "send")
        self.send = self.transport.start()
        self.addCleanup(self.transport.stop)
        self.entries = discover([ROOT / "boats/heron/experiments/motion_timed"])

    def tearDown(self):
        self.service.takeover("Test cleanup")

    def client(self, fail=False):
        test = self

        class Client:
            elapsed = 0
            segments = 0

            @contextmanager
            def armed_manual(self):
                test.vehicle._confirmed_armed = True
                try:
                    yield self
                finally:
                    test.vehicle._confirmed_armed = False

            @contextmanager
            def control(self):
                test.now += .31
                test.vehicle._last_vehicle_heartbeat = test.now
                test.service.tick()
                self.id = test.service.acquire({"mode": "live", "kind": "axes"})["id"]
                try:
                    yield self
                finally:
                    test.service.release()

            def execute(self, operation, **data):
                if fail:
                    raise RuntimeError("Injected input failure")
                getattr(test.service, operation)({"id": self.id, **data})
                profile = test.service.profile
                self.elapsed += profile.duration
                self.segments += 1
                # Advance backend time directly: no live vehicle and no wall-clock wait.
                for fraction in (0, .25, .5, .75, 1):
                    test.now = test.service.profile_started + fraction * profile.duration
                    test.vehicle._last_vehicle_heartbeat = test.now
                    test.service.tick()
                    test.assertTrue(test.service.busy)
                test.assertTrue(test.service.owner["profile_complete"])
                test.assertEqual(test.service.target["throttle"], 0)
                test.assertEqual(test.service.target["steering"], 0)

            def pulse(self, seconds, **target):
                self.execute("pulse", duration_s=seconds, target=target)

            def play(self, frames):
                self.execute("play", keyframes=frames)

        return Client()

    def test_all_ten_cases_finish_without_gps_and_neutralize(self):
        self.assertEqual(len(self.entries), 10)
        self.assertEqual((self.vehicle.lat, self.vehicle.lon), (0, 0))
        for entry in self.entries:
            with self.subTest(case=entry.name):
                client = self.client()
                entry.function(client)
                expected = {"01_acceleration": 20, "04_zigzag": 40}.get(entry.path.stem, 10)
                self.assertEqual(client.elapsed, expected)
                self.assertEqual(client.segments, 4 if entry.path.stem == "04_zigzag" else 1)
                self.assertFalse(self.service.busy)
                self.assertFalse(self.vehicle._confirmed_armed)
                self.send.assert_called_with("manual_control", 1, 0, 0, 0, 0, 0)

    def test_failed_inputs_release_and_disarm_every_case(self):
        for entry in self.entries:
            with self.subTest(case=entry.name):
                with self.assertRaisesRegex(RuntimeError, "Injected input failure"):
                    entry.function(self.client(fail=True))
                self.assertFalse(self.service.busy)
                self.assertFalse(self.vehicle._confirmed_armed)
                self.send.assert_called_with("manual_control", 1, 0, 0, 0, 0, 0)


if __name__ == "__main__":
    unittest.main()
