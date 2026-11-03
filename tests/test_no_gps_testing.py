"""Exercise the explicit GPS bypass with a mocked vehicle, never real motors."""
import unittest
import time
from types import SimpleNamespace
from unittest.mock import patch
from backend.batch import live_blockers, discover
from backend.paths import ROOT
from backend.controller.vehicle_control import VehicleController
from backend.controller.runtime import ControllerRuntime
from backend.control.adapter import Adapter, PARAMETERS
from backend.control.planning import Waypoint


class NoGpsTests(unittest.TestCase):
    def setUp(self):
        with patch('backend.mavlink.transport.SessionRecorder'):
            self.c = VehicleController()
        c = self.c
        c.connected = c._confirmed_armed = True
        c.mode_str = c._confirmed_mode = 'MANUAL'
        c._last_vehicle_heartbeat = time.monotonic()
        c.master = SimpleNamespace(target_system=1,target_component=1)
        values = {"PILOT_STEER_TYPE":1,"SERVO1_FUNCTION":73,"SERVO3_FUNCTION":74,
                  "RCMAP_ROLL":1,"RCMAP_THROTTLE":3,"RC1_MIN":1000,"RC1_TRIM":1500,"RC1_MAX":2000,
                  "RC1_REVERSED":0,"RC3_MIN":1000,"RC3_TRIM":1500,"RC3_MAX":2000,"RC3_REVERSED":0}
        c._control_parameters = {k:(v,time.monotonic()) for k,v in values.items()}
        self.runtime = ControllerRuntime(c,SimpleNamespace(last_error=None),inputs=False)
        self.runtime.testing_without_gps = True
        self.service = self.runtime.programmatic
        self.parameters = patch.object(Adapter,'parameters')
        self.parameters.start()
        self.send = patch.object(c.transport,'send').start()

    def tearDown(self):
        with self.runtime._commands: self.service.takeover('Test finished')
        patch.stopall()

    def test_nogps_raw_controls_allow_zero_position_but_still_send_finite_inputs(self):
        info = self.service.acquire({'mode':'live','kind':'thrusters'})
        self.service.set_target({'id':info['id'],'target':{'left':.2,'right':-.2}})
        self.service.tick()
        self.send.assert_any_call('rc_channels_override',1,1,1600,0,1400,0,0,0,0,0)
        self.assertEqual(self.service.owner['lat'],0)
        self.assertFalse(self.service.owner['geofence_enabled'])
        self.assertTrue(self.service.owner['gps_bypass'])

    def test_heartbeat_arming_mode_and_expiry_are_not_bypassed(self):
        self.c._confirmed_armed = False
        with self.assertRaises(ValueError): self.service.acquire({'mode':'live'})
        self.c._confirmed_armed = True
        info = self.service.acquire({'mode':'live'})
        self.service.set_target({'id':info['id'],'target':{'throttle':.2,'steering':0}})
        self.service.tick(time.monotonic()+1)
        self.assertFalse(self.service.busy)
        self.assertIn('expired',self.service.last['reason'])

    def test_navigation_and_long_profiles_are_rejected(self):
        for operation in ('navigate','follow','execute'):
            with self.assertRaisesRegex(ValueError,'disabled'):
                self.service.command(operation,{})
        with self.assertRaisesRegex(ValueError,'disabled'): self.runtime.command('start',{})
        info = self.service.acquire({'mode':'live','kind':'thrusters'})
        with self.assertRaisesRegex(ValueError,'60 seconds'):
            self.service.play({'id':info['id'],'keyframes':[(0,{'left':0,'right':0}),(61,{'left':1,'right':1})]})
        self.service.tick(self.service._started+61)
        self.assertIn('60 s',self.service.last['reason'])

    def test_normal_mode_still_requires_position(self):
        self.runtime.testing_without_gps = False
        with self.assertRaisesRegex(ValueError,'GPS fix'):
            self.service.acquire({'mode':'live'})

    def test_readiness_bypass_only_removes_gps_blockers(self):
        state = dict(connected=True,mode='MANUAL',armed=True,fix=1,satellites=0,lat=0,lon=0)
        self.assertTrue(live_blockers(state))
        self.assertEqual(live_blockers(state,no_gps=True),[])
        state['armed'] = False
        self.assertEqual(live_blockers(state,no_gps=True),['vehicle disarmed'])

    def test_all_nine_cases_have_finite_nogps_input_sequences(self):
        from contextlib import contextmanager
        class BenchClient:
            testing_without_gps = True
            def __init__(self): self.frames=[]
            @contextmanager
            def control(self,**kwargs): yield self
            def play(self,frames): self.frames.append(frames)
            def run(self,*args,**kwargs): raise AssertionError('No-GPS case attempted waypoint execution')
        for entry in discover([ROOT/'experiments']):
            client = BenchClient()
            entry.function(client)
            self.assertEqual(len(client.frames),1)
            self.assertLessEqual(client.frames[0][-1][0],60)


if __name__ == '__main__': unittest.main()
