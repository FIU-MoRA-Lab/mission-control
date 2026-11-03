"""Verify encoded inputs for standard/paddle modes using the reported calibration."""
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch
from backend.control.adapter import Adapter
from backend.control.planning import Waypoint
from backend.controller.vehicle_control import VehicleController


class CalibratedControlTests(unittest.TestCase):
    def setUp(self):
        with patch('backend.mavlink.transport.SessionRecorder'):
            self.c = VehicleController()
        c=self.c
        c.connected=c._confirmed_armed=True
        c._confirmed_mode='MANUAL'
        c._last_vehicle_heartbeat=time.monotonic()
        c.master=SimpleNamespace(target_system=1,target_component=1)
        self.values={'PILOT_STEER_TYPE':0,'SERVO1_FUNCTION':73,'SERVO3_FUNCTION':74,
                     'RCMAP_ROLL':1,'RCMAP_THROTTLE':3,'RC1_MIN':1100,'RC1_TRIM':1500,'RC1_MAX':1901,
                     'RC1_REVERSED':0,'RC3_MIN':1100,'RC3_TRIM':1500,'RC3_MAX':1900,'RC3_REVERSED':0}
        self.adapter=Adapter(c,'live',Waypoint(0,0),0)
        self.adapter.testing_without_gps=True

    def mapping(self, **changes):
        self.values.update(changes)
        self.c._control_parameters={k:(v,time.monotonic()) for k,v in self.values.items()}
        self.adapter._verified_mapping=None

    def encoded(self,left,right):
        self.adapter.parameters('thrusters')
        with patch.object(self.c.transport,'send') as send:
            self.adapter.apply({'kind':'thrusters','left':left,'right':right},.02)
        return send.call_args.args

    def test_default_mode_uses_mean_and_difference_with_vehicle_calibration(self):
        self.mapping()
        self.assertEqual(self.encoded(.2,.2),('rc_channels_override',1,1,1500,0,1580,0,0,0,0,0))
        self.assertEqual(self.encoded(.2,-.2),('rc_channels_override',1,1,1580,0,1500,0,0,0,0,0))
        self.assertEqual(self.encoded(.4,.2),('rc_channels_override',1,1,1540,0,1620,0,0,0,0,0))

    def test_reverse_compensates_default_pilot_steering_direction(self):
        self.mapping()
        self.assertEqual(self.encoded(-.4,-.2),('rc_channels_override',1,1,1540,0,1380,0,0,0,0,0))

    def test_paddle_mode_sends_left_right_without_inverse_mix(self):
        self.mapping(PILOT_STEER_TYPE=1)
        self.assertEqual(self.encoded(.2,-.2),('rc_channels_override',1,1,1580,0,1420,0,0,0,0,0))

    def test_reversal_and_nonstandard_trim_encode_neutral_correctly(self):
        self.mapping(RC1_REVERSED=1,RC3_TRIM=1490)
        self.encoded(.2,-.2)
        with patch.object(self.c.transport,'send') as send: self.adapter.neutral()
        self.assertEqual(send.call_args.args,('rc_channels_override',1,1,1500,0,1490,0,0,0,0,0))

    def test_bad_mapping_and_calibration_still_rejected_and_changes_revoke_binding(self):
        self.mapping(RC1_MIN=1500)
        with self.assertRaises(ValueError): self.adapter.parameters('thrusters')
        self.mapping(RC1_MIN=1100)
        self.adapter.parameters('thrusters')
        self.c._control_parameters['PILOT_STEER_TYPE']=(1,time.monotonic())
        with self.assertRaisesRegex(ValueError,'changed'): self.adapter.parameters('thrusters')


if __name__ == '__main__': unittest.main()
