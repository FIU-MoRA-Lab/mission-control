"""Bench tests select a motor directly, including rejection and stop paths."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pymavlink import mavutil

from boats.heron.backend.control.motor_test import MotorTestAdapter
from boats.heron.backend.control.planning import Waypoint
from boats.heron.backend.controller.vehicle_control import VehicleController


class IsolatedMotorTests(unittest.TestCase):
    def setUp(self):
        with patch('boats.heron.backend.mavlink.transport.SessionRecorder'):
            self.c = VehicleController()
        self.c.connected = True
        self.c.autopilot_type = mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA
        self.c.vehicle_type = mavutil.mavlink.MAV_TYPE_SURFACE_BOAT
        self.c._confirmed_mode = 'MANUAL'
        self.c._last_vehicle_heartbeat = time.monotonic()
        self.c.master = SimpleNamespace(target_system=1, target_component=1)
        self.adapter = MotorTestAdapter(self.c, 'live', Waypoint(0, 0), 0)
        self.adapter.testing_without_gps = True
        self.send = patch.object(self.c.transport, 'send').start()
        self.addCleanup(patch.stopall)

    def test_right_test_never_sends_a_mixed_manual_control_command(self):
        self.adapter.start({'left':0, 'right':.99}, 20)
        self.adapter.apply({'kind':'motor_test', 'left':0, 'right':.99}, .02)
        self.send.assert_called_once_with('command_long', 1, 1,
            mavutil.mavlink.MAV_CMD_DO_MOTOR_TEST, 0, 4, 0, 99, 20, 0, 0, 0)

    def test_left_reverse_and_cleanup_target_only_the_selected_motor(self):
        self.adapter.start({'left':-.4, 'right':0}, 20)
        self.send.assert_called_once_with('command_long', 1, 1,
            mavutil.mavlink.MAV_CMD_DO_MOTOR_TEST, 0, 3, 0, -40, 20, 0, 0, 0)
        self.send.reset_mock()
        self.adapter.neutral()
        self.assertEqual(self.send.call_args_list[0].args,
            ('command_long', 1, 1, mavutil.mavlink.MAV_CMD_DO_MOTOR_TEST, 0,
             3, 0, 0, 0, 0, 0, 0))
        self.send.reset_mock()
        self.adapter.neutral()
        # Repeated neutral ticks must never restart/arm a native motor test.
        self.send.assert_called_once_with('manual_control', 1, 0, 0, 0, 0, 0)

    def test_rejects_armed_both_motors_and_unbounded_tests(self):
        for target, duration, armed in (({'left':.2,'right':.2},20,False),
                                        ({'left':0,'right':.2},31,False),
                                        ({'left':0,'right':.2},20,True)):
            self.c._confirmed_armed = armed
            with self.assertRaises(ValueError):
                self.adapter.start(target, duration)
        self.send.assert_not_called()

    def test_rejects_other_vehicle_types_and_accepts_integer_percent_roundoff(self):
        self.c.vehicle_type = mavutil.mavlink.MAV_TYPE_QUADROTOR
        with self.assertRaisesRegex(ValueError, 'ArduRover'):
            self.adapter.start({'left':0,'right':.29},20)
        self.send.assert_not_called()
        self.c.vehicle_type = mavutil.mavlink.MAV_TYPE_SURFACE_BOAT
        self.adapter.start({'left':0,'right':.29},20)
        self.assertEqual(self.send.call_args.args[7], 29)

    def test_command_rejection_and_missing_ack_fail_the_test(self):
        self.adapter.start({'left':0,'right':.99},20)
        self.c._motor_test_ack = (mavutil.mavlink.MAV_RESULT_DENIED, time.monotonic())
        with self.assertRaisesRegex(ValueError, 'rejected'):
            self.adapter.state()
        self.c._motor_test_ack = None
        with patch('boats.heron.backend.control.motor_test.time.monotonic', return_value=self.adapter.started_at+2.1):
            with self.assertRaisesRegex(ValueError, 'acknowledge'):
                self.adapter.state()

    def test_kill_and_link_loss_allow_stop_but_prevent_start(self):
        self.c.emergency_stopped = True
        with self.assertRaisesRegex(ValueError, 'kill switch'):
            self.adapter.start({'left':0,'right':.99},20)
        self.adapter.neutral()
        self.send.assert_called_once_with('manual_control', 1, 0, 0, 0, 0, 0)
        self.send.reset_mock()
        self.c.emergency_stopped = False
        self.c.connected = False
        with self.assertRaisesRegex(ValueError, 'heartbeat'):
            self.adapter.start({'left':0,'right':.99},20)
        self.send.assert_not_called()

    def test_firmware_disarm_at_deadline_is_normal_but_early_disarm_fails(self):
        self.adapter.start({'left':0,'right':.99},20)
        self.c._motor_test_ack = (mavutil.mavlink.MAV_RESULT_ACCEPTED, time.monotonic())
        self.c._confirmed_armed = True
        self.adapter.state()
        self.c._confirmed_armed = False
        with self.assertRaisesRegex(ValueError, 'disarmed'):
            self.adapter.state()
        with patch('boats.heron.backend.control.motor_test.time.monotonic', return_value=self.adapter.deadline):
            with patch.object(self.c, 'check_vehicle_link', return_value=True):
                self.adapter.state()
                self.send.reset_mock()
                self.adapter.apply({'left':0,'right':0}, .02)
                self.send.assert_called_once_with('manual_control', 1, 0, 0, 0, 0, 0)


if __name__ == '__main__':
    unittest.main()
