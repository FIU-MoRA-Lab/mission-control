"""Drift correction must preserve intentional travel and pause manual calibration."""
import queue
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from boats.heron.backend.input.calibration import StickCalibration
from boats.heron.backend.input.joystick import JoystickReader
from boats.heron.backend.controller.runtime import ControllerRuntime


class CalibrationTests(unittest.TestCase):
    def positions(self, *values):
        return dict(zip(StickCalibration.AXES, values or [0.0] * 4))

    def settle(self, calibration, positions, start=0):
        result = None
        for index in range(13):
            result = calibration.update(positions, start + index * 0.1)
        return result

    def test_drift_in_every_direction_is_neutral_before_and_after_learning(self):
        for sign in [-1, 1]:
            calibration = StickCalibration()
            drift = self.positions(*[sign * 0.10] * 4)
            self.assertEqual(calibration.update(drift, 0), self.positions())
            self.assertEqual(self.settle(calibration, drift), self.positions())
            self.assertEqual(calibration.centers, [sign * 0.10] * 4)
            self.assertEqual(calibration.update(self.positions(*[sign * 0.11] * 4), 1.3), self.positions())

    def test_held_direction_does_not_become_center_and_other_stick_can_learn(self):
        calibration = StickCalibration()
        result = self.settle(calibration, self.positions(0.5, -0.5, -0.1, 0.1))
        self.assertGreater(result['left_x'], 0)
        self.assertLess(result['left_y'], 0)
        self.assertEqual(calibration.centers[:2], [0, 0])
        self.assertEqual(calibration.centers[2:], [-0.1, 0.1])

    def test_calibration_keeps_full_travel_and_has_no_deadzone_jump(self):
        calibration = StickCalibration()
        self.settle(calibration, self.positions(0.1, -0.1, 0.1, -0.1))
        for endpoint in [-1.0, 1.0]:
            self.assertEqual(calibration.update(self.positions(*[endpoint] * 4), 2),
                             self.positions(*[endpoint] * 4))
        # Just beyond the dead zone produces a tiny movement, not an 8% step.
        result = calibration.update(self.positions(0.181, -0.181, 0.181, -0.181), 2.1)
        self.assertTrue(all(0 < abs(value) < 0.002 for value in result.values()))

    def test_motion_and_missing_reports_restart_idle_window(self):
        calibration = StickCalibration()
        calibration.update(self.positions(*[0.1] * 4), 0)
        calibration.update(self.positions(*[0.1] * 4), 2)
        self.assertEqual(calibration.centers, [0] * 4)
        for index in range(15):
            value = 0.1 if index % 2 else -0.1
            calibration.update(self.positions(*[value] * 4), 2.1 + index * 0.1)
        self.assertEqual(calibration.centers, [0] * 4)

    def test_noise_widens_deadzone_and_is_filtered(self):
        calibration = StickCalibration()
        for index in range(13):
            value = 0.09 if index % 2 else 0.10
            result = calibration.update(self.positions(*[value] * 4), index * 0.1)
        self.assertEqual(result, self.positions())
        self.assertTrue(all(zone > 0.08 for zone in calibration.deadzones))

    def test_manual_calibration_handles_larger_drift_and_requires_both_sticks_idle(self):
        calibration = StickCalibration()
        calibration.request(now=0)
        self.assertEqual(self.settle(calibration, self.positions(0.2, -0.2, 0.8, 0)), self.positions())
        self.assertTrue(calibration.snapshot()['pending'])
        self.assertEqual(calibration.centers, [0] * 4)
        result = self.settle(calibration, self.positions(0.2, -0.2, -0.2, 0.2), start=1.3)
        self.assertEqual(result, self.positions())
        self.assertFalse(calibration.snapshot()['pending'])
        self.assertEqual(calibration.centers, [0.2, -0.2, -0.2, 0.2])

    def test_manual_calibration_times_out_without_replacing_center(self):
        calibration = StickCalibration()
        calibration.request(now=0)
        calibration.update(self.positions(*[0.8] * 4), 11)
        self.assertFalse(calibration.snapshot()['pending'])
        self.assertIn('timed out', calibration.snapshot()['status'])
        self.assertEqual(calibration.centers, [0] * 4)

    def test_command_pauses_controls_and_requires_explicit_reenable(self):
        backend = ControllerRuntime(Mock(emergency_stopped=False), Mock(), inputs=False)
        backend.gamepad = JoystickReader(queue.Queue())
        backend.controller.mode_str = 'MANUAL'
        backend._publish_live_joystick(0, 0, 'GAMEPAD')
        backend._manual_active = True
        backend.session.manual.set_axes(700, -300, 'GAMEPAD')
        backend.command('calibrate_joystick', {})
        self.assertEqual((backend.session.manual.throttle, backend.session.manual.yaw), (0, 0))
        self.assertFalse(backend._manual_active)
        for action, data in [('manual', {}), ('manual_active', {'active': True})]:
            with self.assertRaisesRegex(ValueError, 'calibration to finish'):
                backend.command(action, data)
        self.settle(backend.gamepad.calibration, self.positions())
        self.assertFalse(backend._manual_active)
        backend.command('manual_active', {'active': True})
        self.assertTrue(backend._manual_active)

    def test_command_requires_connected_reader(self):
        backend = ControllerRuntime(Mock(emergency_stopped=False), Mock(), inputs=False)
        with self.assertRaisesRegex(ValueError, 'Connect a joystick'):
            backend.command('calibrate_joystick', {})

    def test_hid_drift_is_filtered_while_raw_positions_remain_available(self):
        shown = []
        reader = JoystickReader(queue.Queue(maxsize=1), lambda *sample: shown.append(sample))
        raw = [140, 116, 140, 116]
        reports = [raw.copy() for _ in range(14)]
        original = reader.calibration.update
        def calibrate(positions):
            return original(positions, now=(14 - len(reports)) * 0.1)
        class Device:
            def open_path(self, path): pass
            def set_nonblocking(self, enabled): pass
            def close(self): pass
            def read(self, size, timeout_ms):
                report = reports.pop(0)
                if not reports:
                    reader.running = False
                return report
        hid = SimpleNamespace(enumerate=lambda: [{'vendor_id': 0x046d, 'path': b'test'}], device=Device)
        with patch('boats.heron.backend.input.joystick._HID_AVAILABLE', True), \
             patch('boats.heron.backend.input.joystick.hid', hid, create=True), \
             patch.object(reader.calibration, 'update', side_effect=calibrate):
            reader.run()
        self.assertEqual(reader.error, '')
        self.assertTrue(all(sample[:2] == (0, 0) for sample in shown))
        self.assertEqual(shown[-1][3], JoystickReader.decode_positions(raw))
        self.assertEqual(shown[-1][4], raw)
        self.assertEqual(reader.calibration.centers, list(JoystickReader.decode_positions(raw).values()))


if __name__ == '__main__':
    unittest.main()
