"""Raw Rover MANUAL_CONTROL packets do not use app-side RC calibration."""
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from backend.control.adapter import Adapter
from backend.control.planning import Waypoint
from backend.controller.vehicle_control import VehicleController


class RawControlTests(unittest.TestCase):
    def setUp(self):
        with patch('backend.mavlink.transport.SessionRecorder'):
            self.c = VehicleController()
        self.c.connected = self.c._confirmed_armed = True
        self.c._confirmed_mode = 'MANUAL'
        self.c._last_vehicle_heartbeat = time.monotonic()
        self.c.master = SimpleNamespace(target_system=1, target_component=1)
        self.adapter = Adapter(self.c, 'live', Waypoint(0, 0), 0)
        self.adapter.testing_without_gps = True

    def test_individual_thrusters_encode_rover_y_and_z_axes(self):
        for left,right,y,z in ((.2,0,100,100), (0,.2,-100,100),
                               (-.2,0,100,-100), (0,-.2,-100,-100),
                               (.2,-.2,200,0), (.2,.2,0,200), (1,0,500,500)):
            with self.subTest(left=left,right=right), patch.object(self.c.transport,'send') as send:
                self.adapter.apply({'kind':'thrusters','left':left,'right':right},.02)
                send.assert_called_once_with('manual_control',1,0,y,z,0,0)

    def test_missing_stale_or_different_calibration_cannot_change_packets(self):
        for values in ({}, {'RC1_MIN':(800,0),'RC1_TRIM':(1300,0),'RC1_MAX':(2200,0),
                           'RC3_TRIM':(1700,0),'RC1_REVERSED':(1,0),'RC1_DZ':(300,0),
                           'PILOT_STEER_TYPE':(1,0)}):
            self.c._control_parameters = values
            with patch.object(self.c.transport,'send') as send:
                self.adapter.apply({'kind':'axes','throttle':.2,'steering':-.4},.02)
                send.assert_called_once_with('manual_control',1,0,-400,200,0,0)

    def test_neutral_is_raw_zero_and_never_an_rc_override(self):
        self.c._control_parameters = {'RC1_TRIM':(1400,0),'RC3_TRIM':(1600,0)}
        with patch.object(self.c.transport,'send') as send:
            self.adapter.neutral()
            send.assert_called_once_with('manual_control',1,0,0,0,0,0)

    def test_kill_switch_blocks_nonzero_but_allows_raw_neutral(self):
        self.c.emergency_stopped = True
        with patch.object(self.c.transport,'send') as send:
            with self.assertRaisesRegex(ValueError,'Kill switch'):
                self.adapter.apply({'kind':'thrusters','left':.4,'right':.2},.02)
            send.assert_not_called()
            self.adapter.neutral()
            send.assert_called_once_with('manual_control',1,0,0,0,0,0)

    def test_shutting_down_does_not_send_nonzero_input(self):
        self.c.running = False
        with patch.object(self.c.transport,'send') as send:
            with self.assertRaisesRegex(ValueError,'shutting down'):
                self.adapter.apply({'kind':'axes','throttle':.2,'steering':0},.02)
            send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
